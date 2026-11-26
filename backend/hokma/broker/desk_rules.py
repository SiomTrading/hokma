"""The rules of a desk (SPEC-025, delivery 1). A rule is a CONTROL (a value in `desk_limits.config` that the guards of `hokma.exec` apply)
or an INSTRUCTION (a text the desk chief reads and nothing blocks). Everything lives in `desk_limits.config`: no migration.

- A new desk is born with `guards: []`: only the four `ALWAYS` guards run. A desk that already exists keeps its configuration (no `guards` list
  means every guard runs), so for it a control is only a value and the list is never created behind its back.
- The owner (the app) adds, edits, removes and approves. An agent can only PROPOSE (`propose`): the proposal waits in `config.proposed`
  until the owner approves it. An agent can never raise or remove a control: `service.set_limits(..., tighten_only=True)` is unchanged.
- SPEC-026: every control is a restriction or a notice (`config.rule_modes`, see hokma.broker.rule_modes) and is not saved without one. The rules of an
  ACCOUNT (and of one desk in one account) live in hokma.broker.account_rules. Only the owner edits directly; an agent proposes.
- Only a key that a guard reads is in the catalog. Daily loss is in USD (`funding.daily_loss_usd`), the unit the code applies."""
from __future__ import annotations

import math
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hokma.broker import rule_modes, rules, service
from hokma.broker.service import BrokerError, Who
from hokma.db import Database
from hokma.exec.guards import registry

SOURCES = ("chat", "hand", "suggested")
MAX_TEXT = 500
MAX_INSTRUCTIONS = 40
MAX_PROPOSED = 20


@dataclass(frozen=True)
class Control:
    """One row of the catalog. `path` is where the guard reads the value in the config; `scale` turns the unit the owner sees into the
    unit stored (minutes into seconds for the pause). `picker` False: shown when the desk already has it, never offered to add."""
    id: str
    guard: str
    path: tuple[str, ...]
    unit: str
    lines: tuple[str, ...] | None = None
    scale: int = 1
    picker: bool = True
    readonly: bool = False


CONTROLS: tuple[Control, ...] = (
    Control("size_premium", "limits", ("max_premium_usd",), "usd", ("SWO",)),
    Control("size_contracts", "limits", ("funding", "max_contracts"), "contracts", ("DT",)),
    Control("size_margin", "limits", ("max_margin_pct",), "pct", ("SWC",)),
    Control("size_weight", "limits", ("max_weight_pct",), "pct", ("LP",)),
    Control("position_value", "max_position", ("max_position_pct",), "pct"),
    Control("daily_loss", "account", ("funding", "daily_loss_usd"), "usd"),
    Control("drawdown", "account", ("funding", "trailing_drawdown_usd"), "usd"),
    Control("cooldown", "cooldown", ("cooldown_seconds",), "minutes", scale=60),
    Control("events", "event_window", ("event_windows",), "minutes"),
    # read by the account guard but older than this tab: shown when a desk has them, not offered
    Control("daily_loss_pct", "account", ("max_daily_loss_pct",), "pct", picker=False),
    Control("drawdown_pct", "account", ("max_drawdown_pct",), "pct", picker=False),
    Control("capital_floor", "account", ("capital_floor_usd",), "usd", picker=False),
    Control("consistency", "account", ("funding", "consistency_pct"), "pct", picker=False),
    # "only these assets": the whitelist guard, on while `assets_only` is true; the list itself is edited in the settings of the desk
    Control("assets_only", "whitelist", ("assets_only",), "assets", picker=False, readonly=True),
)
BY_ID = {c.id: c for c in CONTROLS}
# every key a guard reads, by guard: a guard type is dropped from `guards` only when none of its keys is left
GUARD_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {}
for _c in CONTROLS:
    GUARD_KEYS.setdefault(_c.guard, ())
    GUARD_KEYS[_c.guard] += (_c.path,)
GUARD_KEYS["account"] += (("funding", "max_contracts"),)      # the account guard also checks the contracts a funded account may hold
del _c

# (suggestion id, control id, value, match, lines): None in lines means every line
SUGGESTED_CONTROLS = (
    ("s_size_premium", "size_premium", 500, None, ("SWO",)),
    ("s_size_contracts", "size_contracts", 2, None, ("DT",)),
    ("s_daily_loss", "daily_loss", 400, None, None),
    ("s_events", "events", 30, "FOMC", None),
)
SUGGESTED_INSTRUCTIONS = tuple((f"s_i{n}", ("SWO",)) for n in range(1, 7))


# ---------------------------------------------------------------------------------------------------- reading the config

def _get(config: Mapping[str, Any], control: Control) -> Any:
    cur: Any = config
    for key in control.path:
        cur = cur.get(key) if isinstance(cur, Mapping) else None
    return cur


def _stored_changes(config: Mapping[str, Any], control: Control, stored: Any) -> dict[str, Any]:
    """The `changes` for `rules.validate_limits` that set (or, with None, remove) one control, keeping the other keys of `funding`."""
    if len(control.path) == 1:
        return {control.path[0]: stored}
    funding = dict(config.get("funding") or {})
    if stored is None:
        funding.pop(control.path[1], None)
    else:
        funding[control.path[1]] = stored
    return {"funding": funding or None}


def _on(config: Mapping[str, Any], control: Control) -> bool:
    guards = config.get("guards")
    return guards is None or control.guard in guards or control.guard in registry.ALWAYS


def _shown(control: Control, stored: Any) -> Any:
    if isinstance(stored, float) and stored.is_integer():
        stored = int(stored)
    if control.scale != 1 and isinstance(stored, (int, float)):
        shown = stored / control.scale
        return int(shown) if float(shown).is_integer() else shown
    return stored


def _line_of(u, desk_id: int) -> str | None:
    row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    return row["line"] if row else None


def _fits(control: Control, line: str | None) -> bool:
    return control.lines is None or line is None or line in control.lines


def _mode_view(config: Mapping[str, Any], rule_id: str) -> dict[str, Any]:
    """The mode of a rule as the owner sees it: a rule saved before SPEC-026 has no entry and is a restriction."""
    entry = (config.get("rule_modes") or {}).get(rule_id) or {}
    return {k: v for k, v in {"mode": entry.get("mode", "restriction"), "on_trip": entry.get("on_trip"), "timeout_min": entry.get("timeout_min")}.items()
            if v is not None}


def active_rules(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rules a desk has now: its real controls (read from the config, a desk that already existed included) and its instructions."""
    sources = config.get("rule_sources") if isinstance(config.get("rule_sources"), Mapping) else {}
    out: list[dict[str, Any]] = []
    for control in CONTROLS:
        if control.id == "events":
            for window in config.get("event_windows") or []:
                rule_id = f"events:{str(window.get('match', '')).lower()}"
                out.append({"id": rule_id, "kind": "control", "control_id": "events", "match": window.get("match"),
                            "value": window.get("minutes_before", 0), "after": window.get("minutes_after", 0), "unit": "minutes",
                            "source": sources.get(rule_id, "existing"), "on": _on(config, control), "scope": "desk", **_mode_view(config, rule_id)})
            continue
        stored = _get(config, control)
        if control.id == "assets_only" and stored is None and "whitelist" in (config.get("guards") or []):
            stored = True                       # the list of assets that a desk is born with: a rule with its own mode (SPEC-026, section 8)
        if stored is None:
            continue
        if stored is False:
            continue
        out.append({"id": control.id, "kind": "control", "control_id": control.id, "value": _shown(control, stored), "unit": control.unit,
                    "source": sources.get(control.id, "existing"), "on": _on(config, control), "readonly": control.readonly,
                    "scope": "desk", **_mode_view(config, control.id)})
    for item in config.get("instructions") or []:
        out.append({"id": item["id"], "kind": "instruction", "text": item["text"], "source": item.get("source", "hand"), "on": True,
                    "created_at": item.get("created_at")})
    return out


def _effect(config: Mapping[str, Any], item: Mapping[str, Any]) -> tuple[bool, Any]:
    """What approving a control proposal does to the desk: (does it loosen a limit that exists, the value the rule has now)."""
    try:
        merged, rule_id = _apply_control(dict(config), item["control_id"], item["value"], item.get("match"), "chat", **_answers(item))
    except BrokerError:
        return False, None
    now = next((r.get("value") for r in active_rules(config) if r["id"] == rule_id), None)
    return bool(rules.loosened_limits(config, merged)), now


def _answers(item: Mapping[str, Any]) -> dict[str, Any]:
    """The answers of the owner to the questions of a rule (mode, what happens at the limit, the wait of a notice) as keyword arguments."""
    return {"mode": item.get("mode"), "on_trip": item.get("on_trip"), "timeout_min": item.get("timeout_min")}


def _card(config: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    """A proposal as the owner sees it: control, value with its unit and word of the event come from the SAVED fields, never from the text."""
    if item.get("kind") != "control":
        return dict(item)
    loosens, now = _effect(config, item)
    return {**item, "unit": BY_ID[item["control_id"]].unit if item["control_id"] in BY_ID else None, "loosens": loosens, "current": now}


def view(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """What the Rules tab shows: catalog, active rules, suggestions, proposals of the chief and the fixed block."""
    with service._db(db).read() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        config = u.broker.get_limits(desk_id)
        line = _line_of(u, desk_id)
        assets = [a.symbol for a in u.desks.list_assets(desk_id)]
        from hokma.broker import account_rules, day_close

        account_rules_view = account_rules.list_for_desk(u, desk_id)
        accounts = [{"account_id": r["account_id"], "name": r["name"], **{k: v for k, v in day_close.state_of(u, r["account_id"]).items() if k != "account_id"}}
                    for r in u.session.query("SELECT a.id AS account_id, a.name FROM desk_account_links l JOIN broker_accounts a ON a.id = l.account_id "
                                             "WHERE l.desk_id = :d ORDER BY a.id", {"d": desk_id})]
    active = active_rules(config)
    taken = {r["control_id"] for r in active if r["kind"] == "control" and r["control_id"] != "events"}
    taken_events = {str(r.get("match", "")).lower() for r in active if r.get("control_id") == "events"}
    used_suggestions = {i.get("suggested_id") for i in config.get("instructions") or []}
    dismissed = set(config.get("dismissed") or [])
    suggested: list[dict[str, Any]] = []
    for sid, cid, value, match, lines in SUGGESTED_CONTROLS:
        if sid in dismissed or (lines is not None and line is not None and line not in lines) or (lines is not None and line is None):
            continue
        if (cid == "events" and (match or "").lower() in taken_events) or (cid != "events" and cid in taken):
            continue
        suggested.append({"id": sid, "kind": "control", "control_id": cid, "value": value, "unit": BY_ID[cid].unit, "match": match,
                          "source": "suggested"})
    for sid, lines in SUGGESTED_INSTRUCTIONS:
        if sid in dismissed or sid in used_suggestions or line not in lines:
            continue
        suggested.append({"id": sid, "kind": "instruction", "source": "suggested"})
    guards = config.get("guards")
    return {"desk_id": desk_id, "line": line, "legacy": guards is None, "fixed": list(registry.ALWAYS),
            "catalog": [{"id": c.id, "guard": c.guard, "unit": c.unit, "lines": list(c.lines) if c.lines else None}
                        for c in CONTROLS if c.picker and _fits(c, line)],
            "active": active, "suggested": suggested, "proposed": [_card(config, p) for p in config.get("proposed") or []],
            "assets": assets, "assets_on": guards is None or "whitelist" in guards, "accounts": accounts, "account_rules": account_rules_view}


# ---------------------------------------------------------------------------------------------------- writing the config

def _owner(who: Who | None, what: str) -> None:
    if who is None or not getattr(who, "owner", False):
        raise service.owner_only(what)


def _bad(detail: str) -> BrokerError:
    return BrokerError("bad_limits", 400, text=detail[:300])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _in_use(config: Mapping[str, Any], guard: str) -> bool:
    for path in GUARD_KEYS.get(guard, ()):
        cur: Any = config
        for key in path:
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if cur is not None and cur is not False:             # assets_only False is the limit off
            return True
    return False


def with_guards(config: dict[str, Any]) -> dict[str, Any]:
    """A desk with a `guards` list runs a control only if its guard type is listed: add the types of every control that has a value.
    Stricter only, so it is safe in every path. A desk without the list runs everything and is left alone."""
    guards = config.get("guards")
    if guards is None:
        return config
    wanted = list(guards)
    for guard in GUARD_KEYS:
        if guard not in wanted and guard not in registry.ALWAYS and _in_use(config, guard):
            wanted.append(guard)
    if wanted != list(guards):
        config["guards"] = registry.validate_types(wanted)
    return config


def _mutate(desk_id: int, fn: Callable[[dict[str, Any]], dict[str, Any]], db: Database | None = None) -> dict[str, Any]:
    with service._db(db).write() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        config = dict(u.broker.get_limits(desk_id))
        new = fn(config)
        try:
            checked = rules.validate_limits(new, {})
        except ValueError as exc:
            raise _bad(str(exc)) from exc
        u.broker.set_limits(desk_id, checked)
        return checked


def _stored(control: Control, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise _bad(f"{control.id} needs a number")
    if control.scale != 1:
        if int(value) != value:
            raise _bad(f"{control.id} must be a whole number of minutes")
        return int(value) * control.scale
    return int(value) if control.unit == "contracts" and float(value).is_integer() else value


def _apply_control(config: dict[str, Any], control_id: str, value: Any, match: str | None, source: str, *, mode: Any = None,
                   on_trip: Any = None, timeout_min: Any = None) -> tuple[dict[str, Any], str]:
    """Put one control in the config. A control that is not there yet needs its mode (SPEC-026): without the answer nothing is saved. One that is
    already there keeps its mode unless a new one is given."""
    control = BY_ID.get(control_id)
    if control is None or not control.picker:
        raise _bad("unknown rule: " + str(control_id)[:40])
    if source not in SOURCES:
        raise _bad("bad source")
    stored = _stored(control, value)
    if control.id == "events":
        word = (match or "").strip()
        if not word:
            raise _bad("an events rule needs the word of the event, like FOMC")
        windows = [w for w in config.get("event_windows") or [] if str(w.get("match", "")).lower() != word.lower()]
        windows.append({"match": word, "minutes_before": stored, "minutes_after": stored})
        changes: dict[str, Any] = {"event_windows": windows}
        rule_id = f"events:{word.lower()}"
    else:
        changes = _stored_changes(config, control, stored)
        rule_id = control.id
    try:
        merged = rules.validate_limits(changes, config)
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    merged["rule_sources"] = {**(config.get("rule_sources") or {}), rule_id: source}
    modes = dict(config.get("rule_modes") or {})
    if mode is None and rule_id in {r["id"] for r in active_rules(config)}:
        pass                                                                    # an edit of the value: the mode it has stays
    else:
        try:
            modes[rule_id] = rule_modes.entry_for(control.id, mode, on_trip, timeout_min)
        except ValueError as exc:
            raise _bad(str(exc)) from exc
    if modes:
        merged["rule_modes"] = modes
    return with_guards(merged), rule_id


def set_control(desk_id: int, control_id: str, value: Any, who: Who, *, match: str | None = None, source: str = "hand",
                mode: Any = None, on_trip: Any = None, timeout_min: Any = None, db: Database | None = None) -> dict[str, Any]:
    """Add a control or change its value. Owner only, and never the extra confirmation to loosen: it is his own edit (SPEC-026, section 7).
    A new control needs ~mode~ (and ~on_trip~ when it closes the day)."""
    _owner(who, "adding or changing a rule")
    if mode == "restriction" and control_id in rule_modes.TRIP_CONTROLS:
        from hokma.exec import live

        with service._db(db).read() as u:
            live.require_available(u, [a.id for a in u.broker.accounts_of_desk(desk_id)])          # A12: the live freeze must work on every account of the desk
    if control_id in rule_modes.TRIP_CONTROLS:
        from hokma.exec import live

        with service._db(db).read() as u:
            live.require_exclusive(u, control_id, [a.id for a in u.broker.accounts_of_desk(desk_id)])          # SPEC-026 v2, decision 1299: the broker does not split the loss by desk
    _mutate(desk_id, lambda cfg: _apply_control(cfg, control_id, value, match, source, mode=mode, on_trip=on_trip, timeout_min=timeout_min)[0], db)
    return view(desk_id, db)


def set_mode(desk_id: int, rule_id: str, who: Who, *, mode: Any, on_trip: Any = None, timeout_min: Any = None,
             db: Database | None = None) -> dict[str, Any]:
    """The owner changes how a rule that exists behaves: restriction or notice, what happens at the limit, the wait of a notice. Owner only."""
    _owner(who, "changing the mode of a rule")

    def change(cfg: dict[str, Any]) -> dict[str, Any]:
        found = next((r for r in active_rules(cfg) if r["id"] == rule_id and r["kind"] == "control"), None)
        if found is None:
            raise BrokerError("rule_not_found", 404, text="that rule does not exist")
        try:
            entry = rule_modes.entry_for(found["control_id"], mode, on_trip, timeout_min)
        except ValueError as exc:
            raise _bad(str(exc)) from exc
        return {**cfg, "rule_modes": {**(cfg.get("rule_modes") or {}), rule_id: entry}}

    _mutate(desk_id, change, db)
    return view(desk_id, db)


def _clean_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise _bad("an instruction needs a text")
    clean = " ".join(text.split())
    if len(clean) > MAX_TEXT:
        raise _bad(f"an instruction is at most {MAX_TEXT} characters")
    return clean


def _append_instruction(config: dict[str, Any], text: str, source: str, suggested_id: str | None = None) -> dict[str, Any]:
    if source not in SOURCES:
        raise _bad("bad source")
    items = list(config.get("instructions") or [])
    if len(items) >= MAX_INSTRUCTIONS:
        raise _bad(f"a desk has at most {MAX_INSTRUCTIONS} instructions")
    if any(i["text"].casefold() == text.casefold() for i in items):
        return config
    item = {"id": "i" + secrets.token_hex(3), "text": text, "source": source, "created_at": _now()}
    if suggested_id:
        item["suggested_id"] = str(suggested_id)[:40]
    return {**config, "instructions": items + [item]}


def add_instruction(desk_id: int, text: Any, who: Who, *, source: str = "hand", suggested_id: str | None = None,
                    db: Database | None = None) -> dict[str, Any]:
    """Add an instruction the chief reads. Owner only. Nothing blocks an order because of it."""
    _owner(who, "adding a rule")
    clean = _clean_text(text)
    _mutate(desk_id, lambda cfg: _append_instruction(cfg, clean, source, suggested_id), db)
    return view(desk_id, db)


def edit_instruction(desk_id: int, rule_id: str, text: Any, who: Who, db: Database | None = None) -> dict[str, Any]:
    _owner(who, "changing a rule")
    clean = _clean_text(text)

    def change(cfg: dict[str, Any]) -> dict[str, Any]:
        items = [dict(i) for i in cfg.get("instructions") or []]
        target = next((i for i in items if i["id"] == rule_id), None)
        if target is None:
            raise BrokerError("rule_not_found", 404, text="that rule does not exist")
        target["text"] = clean
        return {**cfg, "instructions": items}

    _mutate(desk_id, change, db)
    return view(desk_id, db)


def remove_rule(desk_id: int, rule_id: str, who: Who, db: Database | None = None) -> dict[str, Any]:
    """Remove a control (its value and, when no other control needs it, its guard type) or an instruction. Owner only."""
    _owner(who, "removing a rule")

    def change(cfg: dict[str, Any]) -> dict[str, Any]:
        items = list(cfg.get("instructions") or [])
        if any(i["id"] == rule_id for i in items):
            return {**cfg, "instructions": [i for i in items if i["id"] != rule_id] or None}
        head, _, word = rule_id.partition(":")
        control = BY_ID.get(head)
        if control is None or (control.id == "events") != bool(word):
            raise BrokerError("rule_not_found", 404, text="that rule does not exist")
        if control.readonly:
            raise _bad("this rule is edited in the settings of the desk")
        if control.id == "events":
            windows = [w for w in cfg.get("event_windows") or [] if str(w.get("match", "")).lower() != word]
            if len(windows) == len(cfg.get("event_windows") or []):
                raise BrokerError("rule_not_found", 404, text="that rule does not exist")
            changes: dict[str, Any] = {"event_windows": windows or None}
        else:
            if _get(cfg, control) is None:
                raise BrokerError("rule_not_found", 404, text="that rule does not exist")
            changes = _stored_changes(cfg, control, None)
        merged = rules.validate_limits(changes, cfg)
        sources = {k: v for k, v in (cfg.get("rule_sources") or {}).items() if k != rule_id}
        merged["rule_sources"] = sources or None
        modes = {k: v for k, v in (cfg.get("rule_modes") or {}).items() if k != rule_id}
        merged["rule_modes"] = modes or None
        guards = merged.get("guards")
        if guards is not None and control.guard in guards and control.guard not in registry.ALWAYS and not _in_use(merged, control.guard):
            merged["guards"] = [g for g in guards if g != control.guard]
        return {k: v for k, v in merged.items() if v is not None}

    _mutate(desk_id, change, db)
    return view(desk_id, db)


def dismiss(desk_id: int, suggested_id: str, who: Who, db: Database | None = None) -> dict[str, Any]:
    """Hide a suggestion in this desk. Owner only."""
    _owner(who, "changing the rules")
    known = {s[0] for s in SUGGESTED_CONTROLS} | {s[0] for s in SUGGESTED_INSTRUCTIONS}
    if suggested_id not in known:
        raise BrokerError("rule_not_found", 404, text="that suggestion does not exist")
    _mutate(desk_id, lambda cfg: {**cfg, "dismissed": sorted(set(cfg.get("dismissed") or []) | {suggested_id})}, db)
    return view(desk_id, db)


# ---------------------------------------------------------------------------------------------------- the chief proposes

def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def _number(text: str) -> float | None:
    found = re.search(r"\d[\d.,]*", text)
    if not found:
        return None
    raw = found.group(0).rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


EVENT_WORDS = ("FOMC", "CPI", "NFP", "PCE", "PPI", "GDP")


def classify(text: str, line: str | None) -> dict[str, Any] | None:
    """The control a sentence names, or None when it is only an instruction. Deliberately narrow: a sentence that does not clearly
    name a control of the catalog, with its number, is an instruction the chief reads, not a control that blocks."""
    low = _plain(text)
    n = _number(low)
    if n is None:
        return None
    found: tuple[str, str | None] | None = None
    if re.search(r"prima|premium", low):
        found = ("size_premium", None)
    elif re.search(r"(perdida|loss).*(diari|dia\b|daily|day)|(diari|daily).*(perdida|loss)", low):
        found = ("daily_loss", None)
    elif re.search(r"drawdown|caida desde|desde el pico|from the peak", low):
        found = ("drawdown", None)
    elif re.search(r"pausa|cooldown|entre ordenes|between orders", low):
        n = max(1.0, round(n / 60)) if re.search(r"segund|second", low) else n
        found = ("cooldown", None)
    elif re.search(r"contrato|contract", low):
        found = ("size_contracts", None)
    elif re.search(r"posicion.*%|position.*%|%.*posicion|%.*position", low):
        found = ("position_value", None)
    else:
        word = next((w for w in EVENT_WORDS if re.search(rf"\b{w.lower()}\b", low)), None)
        if word:
            found = ("events", word)
    if found is None:
        return None
    control = BY_ID[found[0]]
    if not _fits(control, line):
        return None
    return {"control_id": control.id, "value": int(n) if float(n).is_integer() else n, "match": found[1]}


def propose(desk_id: int, text: Any, *, control_id: str | None = None, value: Any = None, match: str | None = None,
            mode: Any = None, on_trip: Any = None, timeout_min: Any = None, account_id: int | None = None, only_desk: bool = False,
            db: Database | None = None) -> dict[str, Any]:
    """The chief (or anyone) PROPOSES a rule. It is saved in `config.proposed` and nothing is applied: the owner approves it in the tab.
    The control and its value ALWAYS come from the text (`classify`), never from what the caller sends: with `control_id` the caller only
    says what it means, and a `control_id`, `value` or `match` that does not agree with the sentence is refused. What the owner reads in
    the card is what is saved, and the sentence says the same.

    SPEC-026: a proposal that is a control carries the answers of the owner: ~mode~ (restriction or notice, ALWAYS asked), ~on_trip~ (what
    happens when a loss limit is reached, asked for loss and drawdown in restriction) and ~timeout_min~ (the wait of a notice). Without
    them it is refused with the question the agent must ask. With ~account_id~ the rule is one of that account (~only_desk~: only this desk's part)."""
    clean = _clean_text(text)
    with service._db(db).read() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        line = _line_of(u, desk_id)
    if control_id is not None:
        control = BY_ID.get(control_id)
        if control is None or not control.picker or not _fits(control, line):
            raise _bad("unknown rule for this desk: " + str(control_id)[:40])
        _apply_control({}, control_id, value, match, "chat", mode=mode, on_trip=on_trip, timeout_min=timeout_min)      # checked now, so the approval cannot fail later
        found = classify(clean, line)
        agrees = (found is not None and found["control_id"] == control_id and found["value"] == value
                  and (found["match"] or "").casefold() == (match or "").strip().casefold())
        if not agrees:
            raise _bad("the control or its value does not agree with the sentence: write the number in the sentence, like \"at most 500 dollars of premium\"")
        item: dict[str, Any] = {"kind": "control", **found}
    else:
        found = classify(clean, line)
        item = {"kind": "control", **found} if found else {"kind": "instruction"}
        if found:
            try:
                _apply_control({}, found["control_id"], found["value"], found["match"], "chat", mode=mode, on_trip=on_trip, timeout_min=timeout_min)
            except BrokerError as exc:
                if exc.text and ("mode_required" in exc.text or "on_trip_required" in exc.text):
                    raise                                                # the number is a control: the owner has to answer the question first
                item = {"kind": "instruction"}
    item.update({"id": "p" + secrets.token_hex(3), "text": clean, "created_at": _now()})
    if item["kind"] == "control":
        item.update({k: v for k, v in _answers({"mode": mode, "on_trip": on_trip, "timeout_min": timeout_min}).items() if v is not None})
        if account_id is not None:
            from hokma.broker import account_rules

            account_rules._control(item["control_id"])
            with service._db(db).read() as u:
                account_rules._linked(u, desk_id, int(account_id))
            item.update({"account_id": int(account_id), "only_desk": bool(only_desk)})
            try:
                entry = rule_modes.entry_for(item["control_id"], mode, on_trip, timeout_min)
            except ValueError as exc:
                raise _bad(str(exc)) from exc
            item["timeout_min"] = entry.get("timeout_min")
            item = {k: v for k, v in item.items() if v is not None}

    if item["kind"] == "control" and item["control_id"] in rule_modes.TRIP_CONTROLS and (item.get("account_id") is None or item.get("only_desk")):
        from hokma.exec import live

        with service._db(db).read() as u:                                       # a rule of the desk in a shared account is refused when it is proposed, not when it is approved
            mine = [a.id for a in u.broker.accounts_of_desk(desk_id)] if item.get("account_id") is None else [int(item["account_id"])]
            live.require_exclusive(u, item["control_id"], mine)

    def add(cfg: dict[str, Any]) -> dict[str, Any]:
        queue = list(cfg.get("proposed") or [])
        if len(queue) >= MAX_PROPOSED:
            raise _bad(f"there are already {MAX_PROPOSED} proposals waiting; the owner has to answer them first")
        return {**cfg, "proposed": queue + [item]}

    _mutate(desk_id, add, db)
    return item


def approve(desk_id: int, proposal_id: str, who: Who, db: Database | None = None, *, confirm_loosen: bool = False) -> dict[str, Any]:
    """The owner approves a proposal: it becomes a rule (source "chat") and leaves the queue. A proposal that would raise or take off
    a control the desk already has answers 409 until the owner confirms it ON PURPOSE (`confirm_loosen`), after seeing "from X to Y"."""
    _owner(who, "approving a rule")
    with service._db(db).read() as u:
        waiting = next((p for p in u.broker.get_limits(desk_id).get("proposed") or [] if p["id"] == proposal_id), None)
    if waiting is not None and waiting.get("account_id") is not None:
        _approve_account(desk_id, waiting, db, confirm_loosen=confirm_loosen is True)
        return view(desk_id, db)
    if waiting is not None and waiting.get("kind") == "control" and waiting.get("control_id") in rule_modes.TRIP_CONTROLS:
        from hokma.exec import live

        with service._db(db).read() as u:
            live.require_exclusive(u, waiting["control_id"], [a.id for a in u.broker.accounts_of_desk(desk_id)])          # the desk may have joined a shared account since it was proposed

    def change(cfg: dict[str, Any]) -> dict[str, Any]:
        queue = list(cfg.get("proposed") or [])
        item = next((p for p in queue if p["id"] == proposal_id), None)
        if item is None:
            raise BrokerError("rule_not_found", 404, text="that proposal does not exist")
        rest = {**cfg, "proposed": [p for p in queue if p["id"] != proposal_id] or None}
        rest = {k: v for k, v in rest.items() if v is not None}
        if item["kind"] == "control":
            applied = _apply_control(rest, item["control_id"], item["value"], item.get("match"), "chat", **_answers(item))[0]
            if rules.loosened_limits(cfg, applied) and confirm_loosen is not True:
                raise BrokerError("loosens_limit", 409, text="this proposal loosens a control the desk already has: confirm it to apply it")
            return applied
        return _append_instruction(rest, item["text"], "chat")

    _mutate(desk_id, change, db)
    return view(desk_id, db)


def _approve_account(desk_id: int, item: Mapping[str, Any], db: Database | None, *, confirm_loosen: bool) -> None:
    """Approve a proposal about an account: the rule goes to account_rules and the proposal leaves the queue, in one unit."""
    from hokma.broker import account_rules

    with service._db(db).write() as u:
        cfg = dict(u.broker.get_limits(desk_id))
        account_rules.apply(u, desk_id, int(item["account_id"]), item["control_id"], item["value"], mode=item.get("mode"), on_trip=item.get("on_trip"),
                            timeout_min=item.get("timeout_min"), only_desk=bool(item.get("only_desk")), source="chat", confirm_loosen=confirm_loosen)
        queue = [p for p in cfg.get("proposed") or [] if p["id"] != item["id"]]
        left = {**cfg, "proposed": queue or None}
        u.broker.set_limits(desk_id, rules.validate_limits({k: v for k, v in left.items() if v is not None}, {}))


def reject(desk_id: int, proposal_id: str, who: Who, db: Database | None = None) -> dict[str, Any]:
    _owner(who, "answering a proposed rule")

    def change(cfg: dict[str, Any]) -> dict[str, Any]:
        queue = list(cfg.get("proposed") or [])
        if not any(p["id"] == proposal_id for p in queue):
            raise BrokerError("rule_not_found", 404, text="that proposal does not exist")
        return {**cfg, "proposed": [p for p in queue if p["id"] != proposal_id] or None}

    _mutate(desk_id, change, db)
    return view(desk_id, db)

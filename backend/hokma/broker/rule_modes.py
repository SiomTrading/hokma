"""The mode of a rule (SPEC-026, piece 3390a). Every control of a desk or of an account is a RESTRICTION (it blocks) or a NOTICE (it
only asks the owner, and the order waits for his answer). The mode is the owner's to choose and nothing here picks it for him: a control
is not saved without one.

Where it lives: the modes of the controls of a desk are in `desk_limits.config.rule_modes`, a map {rule id: entry}. An entry is
{"mode": "restriction" | "notice", "on_trip": ..., "timeout_min": N}. A control saved before this piece has no entry and is a restriction.
`on_trip` is only for the controls that close the day (daily loss and drawdown) in restriction; `timeout_min` only for a notice."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MODES = ("restriction", "notice")
ON_TRIPS = ("block_entries", "block_and_flatten", "block_and_flatten_all")
# a bigger step closes more: the order of strictness of what happens when the limit is reached
ON_TRIP_RANK = {name: n for n, name in enumerate(ON_TRIPS)}
# the controls whose limit closes the day of the account, so the owner says what happens to the open positions
TRIP_CONTROLS = ("daily_loss", "daily_loss_pct", "drawdown", "drawdown_pct")
DEFAULT_TIMEOUT_MIN = 30
MAX_TIMEOUT_MIN = 1440
MAX_ENTRIES = 80

# (event_window is not here: its rule id is `events:{word}`, and the guard gives it in a flag `rule:events:{word}`)
# the code of a block (or the key of a clamp) that a guard gives, and the control it comes from. A code that is not here is a fact the
# guard could not measure (book_unavailable, no_base, unpriced...): it never turns into a notice, it blocks.
CODE_RULE = {
    "daily_loss": "daily_loss", "daily_loss_pct": "daily_loss_pct", "trailing_drawdown": "drawdown", "max_drawdown_pct": "drawdown_pct",
    "capital_floor": "capital_floor", "consistency": "consistency", "funding_max_contracts": "size_contracts",
    "cooldown": "cooldown", "max_position_pct": "position_value", "symbol_not_allowed": "assets_only",
    "limit_max_contracts": "size_contracts", "limit_max_premium": "size_premium", "limit_max_margin": "size_margin",
    "limit_max_weight": "size_weight",
}


# The ONE record of which keys of the limits each control rule id governs (the same paths as hokma.broker.desk_rules.CONTROLS, kept here so
# the guards need not import it). A control may have several: the fixed value and, for a loss or a drawdown, its scale of steps (SPEC-026, A8).
# without_notices takes ALL of them out and _present looks at ANY of them, so a rule that is only steps is neither hidden nor unrestricted.
CONTROL_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "size_premium": (("max_premium_usd",),), "size_contracts": (("funding", "max_contracts"),), "size_margin": (("max_margin_pct",),),
    "size_weight": (("max_weight_pct",),), "position_value": (("max_position_pct",),),
    "daily_loss": (("funding", "daily_loss_usd"), ("daily_loss_steps",)),
    "drawdown": (("funding", "trailing_drawdown_usd"), ("drawdown_steps",)),
    "cooldown": (("cooldown_seconds",),), "daily_loss_pct": (("max_daily_loss_pct",),),
    "drawdown_pct": (("max_drawdown_pct",),), "capital_floor": (("capital_floor_usd",),), "consistency": (("funding", "consistency_pct"),),
}


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def without_notices(limits: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """The limits of a desk with every control that is a NOTICE taken out, and whether the list of assets was one of them. A guard run on
    these limits shows only what a RESTRICTION blocks: the pipeline uses it so a notice can never hide a restriction that also trips."""
    out = _thaw(limits or {})
    assets_notice = False
    for rule_id, entry in list((out.get("rule_modes") or {}).items()):
        if entry.get("mode") != "notice":
            continue
        head, _, word = rule_id.partition(":")
        if head == "assets_only":
            assets_notice = True
        elif head == "events":
            windows = [w for w in out.get("event_windows") or [] if str(w.get("match", "")).lower() != word]
            if windows:
                out["event_windows"] = windows
            else:
                out.pop("event_windows", None)
        elif head in CONTROL_KEYS:
            for path in CONTROL_KEYS[head]:
                if len(path) == 1:
                    out.pop(path[0], None)
                elif isinstance(out.get(path[0]), dict):
                    out[path[0]].pop(path[1], None)
    return out, assets_notice


def notice_event_words(limits: Mapping[str, Any] | None) -> set[str]:
    """The words of the events (lowercase) whose rule `events:{word}` is a notice."""
    modes = (limits or {}).get("rule_modes")
    if not isinstance(modes, Mapping):
        return set()
    return {k.partition(":")[2] for k, v in modes.items() if k.startswith("events:") and isinstance(v, Mapping) and v.get("mode") == "notice"}


def _present(limits: Mapping[str, Any], rule_id: str) -> bool:
    head, _, word = rule_id.partition(":")
    if head == "assets_only":
        return limits.get("assets_only") is True or "whitelist" in (limits.get("guards") or [])
    if head == "events":
        return any(str(w.get("match", "")).lower() == word for w in limits.get("event_windows") or [])
    paths = CONTROL_KEYS.get(head)
    if paths is None:
        return False
    for path in paths:
        cur: Any = limits
        for key in path:
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if cur is not None and cur is not False and cur != []:
            return True
    return False


def clean_entry(value: Any, rule_id: str | None = None) -> dict[str, Any]:
    """One entry of `rule_modes`, checked. Raises ValueError with the sentence for the owner. With the `rule_id`, a restriction of the
    controls that close the day (loss, drawdown) must say what happens at the limit (`on_trip`), however the entry arrives."""
    if not isinstance(value, Mapping) or set(value) - {"mode", "on_trip", "timeout_min"}:
        raise ValueError("a rule mode is {mode, on_trip, timeout_min}")
    mode = value.get("mode")
    if mode not in MODES:
        raise ValueError("mode must be restriction or notice")
    out: dict[str, Any] = {"mode": mode}
    if rule_id is not None and mode == "restriction" and rule_id.partition(":")[0] in TRIP_CONTROLS and value.get("on_trip") is None:
        raise ValueError("on_trip_required: a restriction of a loss or a drawdown says what happens at the limit")
    on_trip, timeout = value.get("on_trip"), value.get("timeout_min")
    if on_trip is not None:
        if mode != "restriction" or on_trip not in ON_TRIPS:
            raise ValueError("on_trip is block_entries, block_and_flatten or block_and_flatten_all, and only for a restriction")
        out["on_trip"] = on_trip
    if timeout is not None:
        if mode != "notice" or isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_MIN:
            raise ValueError(f"timeout_min is a whole number of minutes from 1 to {MAX_TIMEOUT_MIN}, and only for a notice")
        out["timeout_min"] = timeout
    return out


def clean_modes(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or len(value) > MAX_ENTRIES or not all(isinstance(k, str) and 0 < len(k) <= 60 for k in value):
        raise ValueError("rule_modes must map a rule id to its mode")
    return {k: clean_entry(v, k) for k, v in value.items()}


def entry_for(control_id: str, mode: Any, on_trip: Any, timeout_min: Any) -> dict[str, Any]:
    """The entry the owner's answers make, with the questions that must be answered: the mode always, `on_trip` for the controls that
    close the day in restriction. A notice takes the default wait when none is given. Raises ValueError with the question to ask."""
    if mode not in MODES:
        raise ValueError("mode_required: ask the owner 'is it a restriction (it blocks) or only a notice (it asks you)?'")
    if mode == "restriction":
        if control_id in TRIP_CONTROLS and on_trip is None:
            raise ValueError("on_trip_required: ask the owner 'when it is reached, do I only block new entries, or also close the open positions "
                             "of that account? Shares and ETFs held long are sold only if you choose block_and_flatten_all'")
        return clean_entry({"mode": mode, **({"on_trip": on_trip} if on_trip is not None else {})}, control_id)
    if on_trip is not None:
        raise ValueError("on_trip is only for a restriction")
    return clean_entry({"mode": mode, "timeout_min": DEFAULT_TIMEOUT_MIN if timeout_min is None else timeout_min})


def mode_of(limits: Mapping[str, Any] | None, rule_id: str) -> str:
    """The mode of a rule of a desk: a restriction unless the owner made it a notice."""
    modes = (limits or {}).get("rule_modes")
    entry = modes.get(rule_id) if isinstance(modes, Mapping) else None
    return entry.get("mode", "restriction") if isinstance(entry, Mapping) else "restriction"


def is_notice(limits: Mapping[str, Any] | None, rule_id: str) -> bool:
    return mode_of(limits, rule_id) == "notice"


def loosened_modes(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[str]:
    """The rules whose mode gets weaker: a restriction that becomes a notice, or a closing that does less (flatten_all to flatten to entries).
    A rule with no entry is an IMPLICIT restriction, so turning it into a notice is a loosening too. The keys of both sides are walked.
    A rule that was removed is not listed here: removing a value is already a loosening of its limit."""
    old_limits, new_limits = before or {}, after or {}
    old, new = old_limits.get("rule_modes") or {}, new_limits.get("rule_modes") or {}
    out = []
    for rule_id in sorted(set(old) | set(new)):
        was, now = old.get(rule_id), new.get(rule_id)
        if now is None:
            continue
        if was is None:
            was_mode = "restriction" if _present(old_limits, rule_id) else None
            was_trip = None
        else:
            was_mode, was_trip = was.get("mode", "restriction"), was.get("on_trip")
        if was_mode == "restriction" and now.get("mode") == "notice":
            out.append(f"rule_modes.{rule_id}")
        elif was_trip is not None and ON_TRIP_RANK.get(now.get("on_trip"), -1) < ON_TRIP_RANK.get(was_trip, -1):
            out.append(f"rule_modes.{rule_id}")
    return out

"""The models of a key connection, from its provider (bug 1173). Hokma keeps no model name of its own.

- The list: the provider's list endpoint (catalog `models_path`) with the student's key, saved in the connection
  (`settings.models`). Read when the student connects or tests, and again in the background at every start. Without
  network the last saved list is used.
- The levels: picked from that list by catalog.pick_tiers and saved in `tier_models` with the AUTO mark. A map the
  student saved (no AUTO mark) is never touched.
- A model that is gone (not in the list any more, or refused with 404 or "not supported") is replaced by the next pick
  for its level, and the student is told: "The model X no longer exists; you now use Y". A model the student chose is
  not replaced: the chat says so and points to the list in Settings > Engines.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hokma.db import get_database
from hokma.engines import catalog, secrets
from hokma.engines.direct import wire

MAX_REJECTED = 50


def _settings(c) -> dict[str, Any]:
    return dict(c.settings or {})


PINNED = "pinned"                  # key in tier_models: the levels the student fixed, comma separated


def pinned(tier_models: dict | None) -> set[str]:
    """The levels whose model the student chose. A map without the AUTO mark is all the student's (a model given when
    the key was added, or a map saved before 0.0.39)."""
    if not tier_models:
        return set()
    if catalog.AUTO not in tier_models:
        return set(catalog.TIERS)
    return {t for t in str(tier_models.get(PINNED) or "").split(",") if t in catalog.TIERS}


def is_manual(tier_models: dict | None) -> bool:
    return pinned(tier_models) == set(catalog.TIERS)


def manual_map(choice: dict[str, str]) -> dict[str, str]:
    """The map saved for the student's choice: {} = all automatic; every level fixed = a plain map; some levels fixed
    = the automatic map with those levels pinned (refresh fills the others)."""
    fixed = {t: m for t, m in choice.items() if t in catalog.TIERS and m and m != catalog.AUTO}
    if not fixed or len(fixed) == len(catalog.TIERS):
        return fixed
    return {**fixed, catalog.AUTO: "1", PINNED: ",".join(t for t in catalog.TIERS if t in fixed)}


def fetch(c) -> list[dict[str, Any]] | wire.WireError | None:
    """The provider's list for this key. The provider's refusal (a bad key, the region...) as a WireError; None when
    there is no list to read (no endpoint) or the answer is not a list."""
    if c.engine != "api":
        return None
    info = catalog.provider(c.provider)
    base = (c.base_url or info.get("base_url") or "").rstrip("/")
    path = info.get("models_path")
    if not base or not path:
        return None
    key = secrets.get(c.id)
    if c.wire == "anthropic":
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        return catalog.normalize_models(wire.get_json(base + path, headers))
    except wire.WireError as exc:
        from hokma.engines import diagnose

        return wire.WireError(exc.status, diagnose.redact(exc.body, key)[:1000])
    except (ValueError, TypeError):
        return None


def list_error(connection_id: int) -> dict[str, Any] | None:
    """How the provider answered the last time its list was asked for, when it refused: {status, body} (no key)."""
    with get_database().read() as u:
        return _settings(u.engine_connections.get(connection_id)).get("list_error")


def refresh(connection_id: int, *, network: bool = True, reject: str = "") -> dict[str, str] | None:
    """Read the list (or use the saved one), pick the levels and save both. `reject`: a model the provider refused,
    kept out of the next picks. Returns the new levels, or None when nothing changed or the map is the student's."""
    with get_database().read() as u:
        c = u.engine_connections.get(connection_id)
    if c.engine != "api":
        return None
    listed = fetch(c) if network else None
    settings = _settings(c)
    if isinstance(listed, wire.WireError):
        # Kept (without the key) so the chat and the test can say why there is no model; the last list stays.
        settings["list_error"] = {"status": listed.status, "body": listed.body}
        if listed.status:                               # no network (status 0) is not news: the saved list is used
            from hokma.engine import errlog

            errlog.log(f"{c.provider or 'api'} model list: HTTP {listed.status}: {listed.body[:500]}", scope="engines")
    elif listed is not None:
        settings["models"] = listed
        settings["models_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        settings.pop("list_error", None)
    rejected = [r for r in settings.get("rejected", []) if isinstance(r, str)]
    if reject and reject not in rejected:
        rejected = (rejected + [reject])[-MAX_REJECTED:]
    settings["rejected"] = rejected
    before = dict(c.tier_models or {})
    picked: dict[str, str] | None = None
    fixed = pinned(before)
    if not is_manual(before):
        picked = catalog.pick_tiers(c.provider, settings.get("models") or [], set(rejected)) or None
        if picked and fixed:                            # the levels the student chose stay as they are
            picked = {**picked, **{t: before[t] for t in fixed if before.get(t)}, PINNED: before[PINNED]}
        if picked == before:
            picked = None
    if picked:
        notices = list(settings.get("notices", []))
        for tier in catalog.TIERS:
            old, new = before.get(tier), picked.get(tier)
            if old and new and old != new and old not in (n.get("from") for n in notices):
                notices.append({"from": old, "to": new})
        settings["notices"] = notices[-10:]
    with get_database().write() as u:
        now = u.engine_connections.get(connection_id)
        if (now.tier_models or {}) != before:           # the student saved a map while the list was read: theirs wins
            u.engine_connections.update(connection_id, settings={**_settings(now), **{k: v for k, v in settings.items()
                                                                                      if k != "notices"}})
            return None
        u.engine_connections.update(connection_id, settings=settings, tier_models=picked if picked else None)
    return picked


def replace(connection_id: int, tier: str, failed: str) -> tuple[str | None, bool]:
    """The model for `tier` after `failed` was refused (or when there is none yet). (model, manual): manual is True
    when the level holds a model the student chose, which Hokma does not replace."""
    with get_database().read() as u:
        c = u.engine_connections.get(connection_id)
    if (tier if tier in catalog.TIERS else "balanced") in pinned(c.tier_models):
        return None, True
    refresh(connection_id, reject=failed)
    with get_database().write() as u:
        now = u.engine_connections.get(connection_id)
        settings = _settings(now)
        if failed:            # this turn tells the student itself: the same notice must not come again next turn
            settings["notices"] = [n for n in settings.get("notices", []) if n.get("from") != failed]
            u.engine_connections.update(connection_id, settings=settings)
    model = catalog.tier_model({"engine": now.engine, "tier_models": now.tier_models}, tier if tier in catalog.TIERS
                               else "balanced")
    return (model if model and model != failed else None), False


def take_notices(connection_id: Any) -> list[str]:
    """The replacements made since the last turn (at start, in the background), as texts for the chat. Once each."""
    if connection_id is None:
        return []
    try:
        with get_database().write() as u:
            c = u.engine_connections.get(connection_id)
            settings = _settings(c)
            notices = settings.pop("notices", [])
            if not notices:
                return []
            u.engine_connections.update(connection_id, settings=settings)
    except Exception:                                   # noqa: BLE001  (a notice never stops a turn)
        return []
    return [replaced_text(n.get("from", ""), n.get("to", "")) for n in notices if n.get("from") and n.get("to")]


def replaced_text(old: str, new: str) -> str:
    from hokma.i18n import t

    return t("engines.model_replaced", old=old, new=new)


def choices(connection_id: int, *, network: bool = True) -> list[str]:
    """The stable chat models of the connection, newest first, for the student to choose from."""
    if network:
        refresh(connection_id)
    with get_database().read() as u:
        c = u.engine_connections.get(connection_id)
    return catalog.chat_models(c.provider, _settings(c).get("models") or [])

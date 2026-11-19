"""Desk logic: a desk is one project (`kind='strategy'`) plus its row, its team, its assets and its folder.

Creation is all or nothing: the project row, the desk row, the team, the assets and the files of the folder go in one
database transaction, and what this call wrote on disk is removed if any step fails. Folders that existed before the
call are never touched.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from hokma.broker import service as broker_service
from hokma.db import Database, DbError, IntegrityViolation, NotFound, ValidationError, get_database
from hokma.db.repos import desks as repo
from hokma.db.repos.base import UNSET
from hokma.db.repos.projects import slugify
from hokma.desks import budget
from hokma.desks import roles as role_data
from hokma.desks import templates as template_data
from hokma.desks.errors import DeskError
from hokma.docs import diskfolders
from hokma.i18n import t
from hokma.kit import safefs
from hokma.kit.integrity import app_root
from hokma.market.yahoo import valid_symbol
from hokma.projects import gitops
from hokma.projects import service as projects
from hokma.projects.errors import ProjectError

log = logging.getLogger("hokma.desks")

# The six agents the kit installs only for money desks (turned off until a desk needs them). Creating a desk from a
# template is the student asking for that team, so the ones the template uses are turned on with it. The agents the
# student already had (hugo, iris, axel, vera) keep the state the student gave them.
DESK_KIT_AGENTS = frozenset({"desk-chief", "signals", "risk", "bull-case", "bear-case", "judge"})
CLAUDE_FILE = "CLAUDE.md"
CLAUDE_TEMPLATE = ("kit", "template", "desks", "CLAUDE.desk.md")
# Used only when the kit template is missing from the install. Same placeholders as the kit file.
FALLBACK_CLAUDE = """# {{desk_name}}

This folder is one money desk of the fund of {{owner_name}}. Simulation only: real money is blocked.

## Mandate

{{mandate}}

## Simulated capital

{{capital}}
"""
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _db(db: Database | None) -> Database:
    return db or get_database()


# ---- validation ------------------------------------------------------------------------------

def _name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= 80:
        raise DeskError("bad_name", 400)
    return name


def _text(value: Any, limit: int, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DeskError(code, 400)
    return value.strip() or None


def _capital(value: Any) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or not repo.MIN_CAPITAL <= value <= repo.MAX_CAPITAL:
        raise DeskError("bad_capital", 400)
    return value


def _objective(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise DeskError("bad_objective", 400)
    return float(value)


def _budget(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DeskError("bad_budget", 400)
    return value


def _model(value: Any) -> str:
    if value not in repo.MODELS:
        raise DeskError("bad_model", 400)
    return value


def _assets(values: Any) -> list[tuple[str, str | None]]:
    """Accepts symbols (`"NQ"`) or `{"symbol", "note"}` items; the order given is the order kept."""
    if not isinstance(values, (list, tuple)) or len(values) > repo.MAX_ASSETS:
        raise DeskError("bad_assets", 400)
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in values:
        symbol, note = (item.get("symbol"), item.get("note")) if isinstance(item, dict) else (item, None)
        if not isinstance(symbol, str) or not 1 <= len(symbol.strip()) <= 40 or symbol.strip() in seen:
            raise DeskError("bad_assets", 400)
        if note is not None and (not isinstance(note, str) or len(note) > 200):
            raise DeskError("bad_assets", 400)
        seen.add(symbol.strip())
        out.append((symbol.strip(), note))
    return out


def check_symbols(values: Any) -> None:
    """Each symbol must have the shape the market routes accept. A list that is not a list is `_assets`' error.
    Shared by the desks API and the chat tools, so both refuse the same symbols."""
    if not isinstance(values, (list, tuple)):
        return
    for item in values:
        symbol = item.get("symbol") if isinstance(item, dict) else item
        if not isinstance(symbol, str) or not valid_symbol(symbol.strip()):
            raise DeskError("bad_symbol", 400)


def validate_assets(values: Any) -> None:
    """Everything `set_assets` checks, without writing: lets a caller refuse a bad list before it changes anything else."""
    check_symbols(values)
    _assets(values)


# ---- views -----------------------------------------------------------------------------------

def _agent_view(row: repo.DeskAgent, names: dict[str, str]) -> dict[str, Any]:
    info = role_data.describe(row.role)
    return {"agent_slug": row.agent_slug, "name": names.get(row.agent_slug, row.agent_slug), "role": row.role,
            "model": row.model, "active": row.active, "can": info["can"], "cannot": info["cannot"]}


def _view(u, desk: repo.Desk, *, detail: bool) -> dict[str, Any]:
    agents = u.desks.list_agents(desk.id)
    names = {a.slug: a.display_name for a in u.agents.list()}
    week = budget.figures(u, desk)
    working = u.desks.working_agents(desk.project_id)
    out = desk.to_dict()
    out["agents_total"] = len(agents)
    out["agents_working"] = sum(1 for a in agents if a.active and a.agent_slug in working)
    out["today"] = 0                    # no simulated trades yet (step 4)
    out["drawdown"] = None
    out["account"] = broker_service.desk_account(u, desk.id)
    out["tokens_week"] = week["used"]
    out["budget_state"] = week["state"]
    if detail:
        out["budget"] = week
        out["agents"] = [_agent_view(a, names) for a in agents]
        out["assets"] = [{"symbol": a.symbol, "note": a.note} for a in u.desks.list_assets(desk.id)]
        out["connections"] = [{"provider_id": c.provider_id, "enabled": c.enabled}
                              for c in u.desks.list_connections(desk.id)]
    return out


def _desk(u, desk_id: int) -> repo.Desk:
    try:
        return u.desks.get(desk_id)
    except NotFound as exc:
        raise DeskError("not_found", 404) from exc


# ---- reads -----------------------------------------------------------------------------------

def list_templates() -> list[dict[str, Any]]:
    return template_data.listing()


def list_desks(*, include_archived: bool = False, db: Database | None = None) -> list[dict[str, Any]]:
    with _db(db).read() as u:
        return [_view(u, d, detail=False) for d in u.desks.list(include_archived=include_archived)]


def get_desk(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    with _db(db).read() as u:
        return _view(u, _desk(u, desk_id), detail=True)


def list_agents(desk_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    """The team with what the Agents tab shows: state (`working`, `idle`, `off`), tokens of the week, last action
    and the three latest actions."""
    with _db(db).read() as u:
        desk = _desk(u, desk_id)
        names = {a.slug: a.display_name for a in u.agents.list()}
        working = u.desks.working_agents(desk.project_id)
        tokens = {r["key"]: r["tokens_in"] + r["tokens_out"] for r in budget.figures(u, desk)["by_agent"]}
        out = []
        for row in u.desks.list_agents(desk_id):
            view = _agent_view(row, names)
            activity = u.desks.recent_activity(desk.project_id, row.agent_slug, 3)
            view["status"] = "off" if not row.active else ("working" if row.agent_slug in working else "idle")
            view["tokens_week"] = tokens.get(row.agent_slug, 0)
            view["last_action"] = activity[0] if activity else None
            view["activity"] = activity
            out.append(view)
        return out


# ---- create ----------------------------------------------------------------------------------

def _claude_text(owner: str, name: str, mandate: str | None, capital: int, currency: str) -> str:
    path = app_root().joinpath(*CLAUDE_TEMPLATE)
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        body = FALLBACK_CLAUDE
    values = {"desk_name": name, "owner_name": owner or "", "capital": f"{capital:,} {currency}",
              "mandate": mandate or t("desks.claude.no_mandate")}
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), body)     # one pass: no re-substitution


def _undo(folder: Path, written: list[str]) -> None:
    """Remove only what this call put on disk, deepest first. Anything unexpected stays."""
    for rel in reversed(written):
        target = folder / rel
        try:
            if target.is_dir() and not target.is_symlink():
                os.rmdir(target)
            else:
                os.unlink(target)
        except OSError:
            pass
    try:
        os.rmdir(folder)
    except OSError:
        pass


def create_desk(name: str, capital: Any, *, focus: str | None = None, template: str = "standard",
                objective_pct: Any = None, mandate: str | None = None, assets: Any = None,
                token_budget: Any = None, db: Database | None = None) -> dict[str, Any]:
    """Create a desk with the agents of `template`. Each agent keeps its default model for its role.

    `token_budget` None means no weekly cap of its own; the suggested figure of the template is data for the screen
    (`list_templates`), not a default applied here.
    """
    database = _db(db)
    clean_name = _name(name)
    clean_capital = _capital(capital)
    clean_focus = _text(focus, 120, "bad_focus")
    clean_mandate = _text(mandate, 4000, "bad_mandate")
    clean_objective = _objective(objective_pct)
    clean_budget = _budget(token_budget)
    clean_assets = _assets(assets if assets is not None else [])
    chosen = template_data.get(template)
    if chosen is None:
        raise DeskError("bad_template", 400)
    with database.read() as u:
        installed = {a.slug for a in u.agents.list()}
        owner = u.profile.get_or_default().owner_name
    if any(m.agent_slug not in installed for m in chosen.members):
        raise DeskError("agent_missing", 409)

    with projects._LOCK:
        root = projects.projects_root(database)
        slug = projects._unique_slug(database, root, slugify(clean_name))
        try:
            folder = projects._make_folder(root, slug)
        except ProjectError as exc:
            raise DeskError("folder_failed", exc.status) from exc
        written: list[str] = []
        try:
            with database.write() as u:
                project = u.projects.create(clean_name, str(folder), slug=slug, kind="strategy",
                                            description=clean_focus)
                desk = u.desks.create(project.id, clean_name, capital=clean_capital, focus=clean_focus,
                                      template=chosen.key, objective_pct=clean_objective, mandate=clean_mandate,
                                      token_budget=clean_budget)
                for member in chosen.members:
                    u.desks.add_agent(desk.id, member.agent_slug, member.role, member.model)
                u.desks.replace_assets(desk.id, clean_assets)
                for sub in projects.SUBFOLDERS:
                    safefs.write_atomic(folder, f"{sub}/.gitkeep", b"")
                    written += [sub, f"{sub}/.gitkeep"]
                text = _claude_text(owner, clean_name, clean_mandate, clean_capital, desk.currency)
                safefs.write_atomic(folder, CLAUDE_FILE, text.encode("utf-8"))
                written.append(CLAUDE_FILE)
        except (DbError, OSError) as exc:
            _undo(folder, written)
            raise DeskError("db_failed" if isinstance(exc, DbError) else "folder_failed", 500) from exc
        except BaseException:
            _undo(folder, written)
            raise
    _turn_on_desk_agents(chosen, database)
    git = gitops.init_repo(folder, t("projects.git.created"))
    diskfolders.ensure_own_folder(project.id, database)
    out = get_desk(desk.id, database)
    out["git"] = {"ok": git.ok, "reason": git.reason}
    return out


def _turn_on_desk_agents(chosen: template_data.DeskTemplate, database: Database) -> None:
    """Turn on, the way Settings does (database and agent files), the desk-only agents this template uses. A failure
    leaves the desk created and that agent off: the Agents tab still offers to turn it on."""
    _switch_on([m.agent_slug for m in chosen.members], database)


def _switch_on(slugs: list[str], database: Database) -> list[str]:
    """Turn on, the way Settings does, the agents among `slugs` that are off. Returns the ones turned on."""
    from hokma.agents.service import AgentService

    if not slugs:
        return []
    with database.read() as u:
        off = [slug for slug in slugs if (found := u.agents.find(slug)) is not None and found.status == "off"]
    service = AgentService(database)
    done = []
    for slug in off:
        try:
            service.set_status(slug, "active")
            done.append(slug)
        except Exception as exc:                                # one agent must not undo a desk that exists already
            log.warning("desk agent %s could not be turned on: %s", slug, exc)
    return done


def ensure_agents_on(project_id: int | None, db: Database | None = None) -> list[str]:
    """An agent that is assigned and active on a desk always works for it: when the project is a desk, the agents of
    its team that are off in Settings are turned on again (so the task runner and `agents_list` see them). Chosen over
    letting the dispatch skip the global switch because the agent file is applied by the same switch."""
    if project_id is None:
        return []
    database = _db(db)
    with database.read() as u:
        desk = u.desks.by_project(project_id)
        slugs = [a.agent_slug for a in u.desks.list_agents(desk.id) if a.active] if desk is not None else []
    return _switch_on(slugs, database)


# ---- delete ----------------------------------------------------------------------------------

def delete_desk(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """Delete a desk: its folder goes to the Trash (restorable); the desk row, its team assignments, assets and
    connections and its project row are removed. Conversations stay in the history (their project becomes none) and
    the student's agents are not touched. The folder moves first: if the database step fails the folder is put back."""
    from hokma.kit.settings import protected_paths
    from hokma.permissions import trash

    database = _db(db)
    with projects._LOCK:
        with database.read() as u:
            desk = _desk(u, desk_id)
            project = u.projects.get(desk.project_id)
            name = desk.name
        root = projects.projects_root(database)
        folder = Path(project.path)
        item_id: int | None = None
        if os.path.lexists(folder):
            try:
                moved = trash.move([str(folder)], t("desks.trash_reason", name=name), db=database,
                                   roots=[str(root)], protect=[str(p) for p in protected_paths()], cwd=str(root),
                                   project_id=None)
            except trash.TrashError as exc:
                raise DeskError("delete_failed", 500) from exc
            item_id = moved[0]["id"]
        try:
            with database.write() as u:
                u.desks.delete(desk_id)
                u.projects.delete(project.id)
        except DbError as exc:
            if item_id is not None:
                try:
                    trash.restore(database, item_id, protect=[str(p) for p in protected_paths()])
                except trash.TrashError as back:
                    log.warning("desk folder could not be put back: %s", back.message)
            raise DeskError("db_failed", 500) from exc
    return {"id": desk_id, "name": name, "trash_id": item_id}


# ---- update ----------------------------------------------------------------------------------

def update_desk(desk_id: int, changes: dict[str, Any], db: Database | None = None) -> dict[str, Any]:
    """`changes` holds only the fields to change: name, focus, capital, objective_pct, mandate, token_budget,
    status. Archiving is `status='archived'`; deleting is `delete_desk`."""
    database = _db(db)
    values: dict[str, Any] = {}
    if "name" in changes:
        values["name"] = _name(changes["name"])
    if "focus" in changes:
        values["focus"] = _text(changes["focus"], 120, "bad_focus")
    if "capital" in changes:
        values["capital"] = _capital(changes["capital"])
    if "objective_pct" in changes:
        values["objective_pct"] = _objective(changes["objective_pct"])
    if "mandate" in changes:
        values["mandate"] = _text(changes["mandate"], 4000, "bad_mandate")
    if "token_budget" in changes:
        values["token_budget"] = _budget(changes["token_budget"])
    if "status" in changes:
        if changes["status"] not in repo.STATUSES:
            raise DeskError("bad_status", 400)
        values["status"] = changes["status"]
    try:
        with database.write() as u:
            _desk(u, desk_id)
            u.desks.update(desk_id, **{k: values.get(k, UNSET) for k in (
                "name", "focus", "capital", "objective_pct", "mandate", "token_budget", "status")})
            return _view(u, u.desks.get(desk_id), detail=True)
    except (IntegrityViolation, ValidationError) as exc:
        raise DeskError("db_failed", 400) from exc


def archive_desk(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    return update_desk(desk_id, {"status": "archived"}, db)


def set_assets(desk_id: int, symbols: Any, db: Database | None = None) -> list[dict[str, Any]]:
    """Replace the list (at most 20). Checking each symbol against the market catalog is the API layer's job."""
    clean = _assets(symbols)
    with _db(db).write() as u:
        _desk(u, desk_id)
        rows = u.desks.replace_assets(desk_id, clean)
    return [{"symbol": a.symbol, "note": a.note} for a in rows]


def reorder_assets(desk_id: int, symbols: Any, db: Database | None = None) -> list[dict[str, Any]]:
    """Put the assets of the desk in the given order (task 3289). The list must hold exactly the symbols the desk has
    now, each once: moving never adds, drops or renames an asset, and the notes stay."""
    wanted = [a[0] for a in _assets(symbols)]
    with _db(db).write() as u:
        _desk(u, desk_id)
        current = u.desks.list_assets(desk_id)
        if sorted(wanted) != sorted(a.symbol for a in current):
            raise DeskError("bad_assets", 409)
        notes = {a.symbol: a.note for a in current}
        rows = u.desks.replace_assets(desk_id, [(symbol, notes[symbol]) for symbol in wanted])
    return [{"symbol": a.symbol, "note": a.note} for a in rows]


# ---- team ------------------------------------------------------------------------------------

def _default_role(agent_slug: str) -> str:
    for member in template_data.TEMPLATES["debate"].members:
        if member.agent_slug == agent_slug:
            return member.role
    return "custom"


def add_agent(desk_id: int, agent_slug: str, model: Any, role: str | None = None,
              db: Database | None = None) -> dict[str, Any]:
    """Bring an existing agent into the desk. An agent turned off in Settings is not turned on here."""
    chosen_model = _model(model)
    chosen_role = role or _default_role(agent_slug)
    if chosen_role not in repo.ROLES or chosen_role == "chief":
        raise DeskError("bad_role", 400)
    with _db(db).write() as u:
        _desk(u, desk_id)
        agent = u.agents.find(agent_slug)
        if agent is None:
            raise DeskError("agent_missing", 404)
        if agent.status == "off":
            raise DeskError("agent_off", 409)
        if u.desks.find_agent(desk_id, agent_slug) is not None:
            raise DeskError("agent_in_desk", 409)
        row = u.desks.add_agent(desk_id, agent_slug, chosen_role, chosen_model)
        return _agent_view(row, {agent.slug: agent.display_name})


def set_agent(desk_id: int, agent_slug: str, *, model: Any = UNSET, active: Any = UNSET,
              db: Database | None = None) -> dict[str, Any]:
    """Change the model of an agent on this desk, or turn it off or on. Other desks and Settings do not change.

    The desk row changes, and when the agent is the chief the conversations of the desk follow its model. Applying
    the model to a task run is the job of P5."""
    values: dict[str, Any] = {}
    if model is not UNSET:
        values["model"] = _model(model)
    if active is not UNSET:
        if not isinstance(active, bool):
            raise DeskError("bad_status", 400)
        values["active"] = active
    with _db(db).write() as u:
        _desk(u, desk_id)
        current = u.desks.find_agent(desk_id, agent_slug)
        if current is None:
            raise DeskError("agent_not_in_desk", 404)
        if current.role == "chief" and values.get("active") is False:
            raise DeskError("chief_required", 409)
        row = u.desks.set_agent(desk_id, agent_slug, **values)
        if current.role == "chief" and "model" in values:
            u.desks.set_conversation_model(_desk(u, desk_id).project_id, agent_slug, values["model"])
        names = {a.slug: a.display_name for a in u.agents.list()}
        return _agent_view(row, names)


def remove_agent(desk_id: int, agent_slug: str, db: Database | None = None) -> None:
    """Remove the link only; the agent itself is global and stays."""
    with _db(db).write() as u:
        _desk(u, desk_id)
        current = u.desks.find_agent(desk_id, agent_slug)
        if current is None:
            raise DeskError("agent_not_in_desk", 404)
        if current.role == "chief":
            raise DeskError("chief_required", 409)
        u.desks.remove_agent(desk_id, agent_slug)


# ---- chat of the desk and connections ------------------------------------------------------

def open_conversation(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """The conversation of the desk: the newest open one with the chief, or a new one. Its model is the chief's model
    on this desk. Messages then go through the normal chat flow."""
    from hokma.engine.dbstore import ENGINE_NAME

    with _db(db).write() as u:
        desk = _desk(u, desk_id)
        if desk.status == "archived":
            raise DeskError("desk_archived", 409)
        chief = u.desks.chief(desk_id)
        if chief is None:
            raise DeskError("chief_required", 409)
        found = [c for c in u.conversations.list(project_id=desk.project_id, archived=False)
                 if c.agent_slug == chief.agent_slug]
        created = not found
        if found:
            conv = found[0]
            if conv.model != chief.model:
                conv = u.conversations.set_model(conv.id, chief.model)
        else:
            default = u.engine_connections.default()
            conv = u.conversations.create(engine=default.engine if default else ENGINE_NAME, title=desk.name,
                                          project_id=desk.project_id, model=chief.model,
                                          agent_slug=chief.agent_slug, project_mode="fixed")
            if default:
                u.engine_connections.attach(conv.id, default.id)
        out = {"conversation_id": conv.id, "desk_id": desk.id, "project_id": desk.project_id,
               "agent_slug": conv.agent_slug, "model": conv.model, "engine": conv.engine, "title": conv.title,
               "created": created}
    ensure_agents_on(out["project_id"], db)
    return out


def _market_service():
    from hokma.connections import runtime

    service = runtime.get_service()
    if not service.loaded:
        service.load()
    return service


def list_connections(desk_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    """The global connections with the choice of this desk. A connection the desk has no row for is not used."""
    service = _market_service()
    with _db(db).read() as u:
        _desk(u, desk_id)
        chosen = {c.provider_id: c.enabled for c in u.desks.list_connections(desk_id)}
    return [{"provider_id": pid, "name": spec.name, "asset_classes": list(spec.asset_classes),
             "needs_key": spec.needs_key, "key_ready": (not spec.needs_key) or service.has_key(pid),
             "enabled_global": service.is_enabled(pid), "enabled": chosen.get(pid, False)}
            for pid, spec in service.catalog.providers.items()]


def set_connection(desk_id: int, provider_id: str, enabled: Any, db: Database | None = None) -> dict[str, Any]:
    """Choose whether this desk uses a provider. The key itself is set in Connections, not here."""
    if _market_service().catalog.get(provider_id) is None:
        raise DeskError("bad_connection", 404)
    if not isinstance(enabled, bool):
        raise DeskError("bad_status", 400)
    with _db(db).write() as u:
        _desk(u, desk_id)
        row = u.desks.set_connection(desk_id, provider_id, enabled)
    return {"provider_id": row.provider_id, "enabled": row.enabled}


# ---- model and budget at run time (SPEC-014 P5) ----------------------------------------------

def model_in(u, project_id: int | None, agent_slug: str | None) -> str | None:
    """The model of `agent_slug` on the desk that owns `project_id`, read inside an open unit; None when the
    project is not a desk or the agent is not on it (then the global model rules)."""
    if project_id is None or not agent_slug:
        return None
    desk = u.desks.by_project(project_id)
    if desk is None:
        return None
    member = u.desks.find_agent(desk.id, agent_slug)
    return member.model if member else None


def model_for(project_id: int | None, agent_slug: str | None, db: Database | None = None) -> str | None:
    """The model the desk gave this agent (`haiku`, `sonnet` or `opus`), or None: outside a desk, or for an agent that
    is not on it, the global model of the agent rules."""
    with _db(db).read() as u:
        return model_in(u, project_id, agent_slug)


def assert_can_run(project_id: int | None, db: Database | None = None, now: Any = None) -> None:
    """The one place that says whether a desk may start something new (a chat turn, a task claim).

    A project that is not a desk always can. A desk whose week is over its token budget cannot: this raises
    `DeskError("desk_budget_exhausted", 409)` and, once per week, writes a `block` entry in the journal of the desk.
    A turn that already runs is never cut: this is only asked before starting. The week changing, or a higher budget,
    lets the desk run again with no state to reset."""
    if project_id is None:
        return
    with _db(db).write() as u:
        desk = u.desks.by_project(project_id)
        if desk is None or not desk.token_budget:
            return
        week = u.usage.week(now=now)
        used = u.desks.usage_total(project_id, since=week.starts_at, until=week.ends_at).tokens
        if budget.state_for(used, desk.token_budget) != "exhausted":
            return
        seen = u.journal.list(kind="block", project_id=project_id, since=week.starts_at, limit=50)
        if not any(isinstance(e.refs, dict) and e.refs.get("reason") == "budget" for e in seen):
            u.journal.add("block", t("desks.budget.blocked", name=desk.name, used=used, budget=desk.token_budget)[:200],
                          actor=budget.SYSTEM, outcome="info", project_id=project_id,
                          refs={"desk": desk.id, "reason": "budget", "week": week.starts_at, "used": used,
                                "budget": desk.token_budget})
    raise DeskError("desk_budget_exhausted", 409)

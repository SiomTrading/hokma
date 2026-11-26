"""Desk logic: a desk is one project (`kind='strategy'`) plus its row, its team, its assets and its folder.

Creation is all or nothing: the project row, the desk row, the team, the assets and the files of the folder go in one
database transaction, and what this call wrote on disk is removed if any step fails. Folders that existed before the
call are never touched.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hokma.broker import desk_accounts as broker_desk_accounts
from hokma.broker import service as broker_service
from hokma.db import Database, DbError, IntegrityViolation, NotFound, ValidationError, get_database
from hokma.db.repos import desks as repo
from hokma.db.repos.base import UNSET
from hokma.db.repos.projects import slugify
from hokma.db.timeutil import from_db_time
from hokma.desks import budget, panels
from hokma.desks import roles as role_data
from hokma.desks import templates as template_data
from hokma.desks.errors import DeskError
from hokma.docs import diskfolders
from hokma.i18n import t
from hokma.kit import safefs
from hokma.kit.integrity import app_root
from hokma.market.yahoo import valid_symbol
from hokma.permissions import trash
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

This folder is one money desk of the fund of {{owner_name}}. Every agent that works here reads this
file first.

## Fixed rule

**Real money moves only through Hokma's order tools.** No agent calls a broker directly, asks for keys or
account numbers, or sends an order any other way. Orders go out only when the owner turned orders on for the
broker of the desk's account, and only within the rules of this desk. Where the rules ask for the owner, the order
waits for the owner's approval. Nothing in this folder can change that.

## Mandate

{{mandate}}

## Capital

{{capital}}

The capital is the amount the desk may commit in its account. An order that would open or increase exposure past it
waits for the owner. Closing or reducing a position is always allowed.
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
    out["today"] = 0                    # orders sent today are read from the broker log, not counted here
    out["drawdown"] = None
    out.update(broker_desk_accounts.of_desk(u, desk.id))               # `accounts` and `strategies` (SPEC-027)
    out["panels"] = panels.resolve(desk.layout)                          # the panels of the screen, in order (SPEC-025, delivery 3)
    out["layout"] = None if desk.layout is None else {"panels": out["panels"]}
    out["tokens_week"] = week["used"]
    out["budget_state"] = week["state"]
    if detail:
        out["budget"] = week
        out["agents"] = [_agent_view(a, names) for a in agents]
        out["assets"] = [{"symbol": a.symbol, "note": a.note} for a in u.desks.list_assets(desk.id)]
        out["connections"] = [{"provider_id": c.provider_id, "enabled": c.enabled}
                              for c in u.desks.list_connections(desk.id)]
        out["limits"] = u.broker.get_limits(desk.id)         # the optional order limits of 18.5; empty means all off
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


RUNS_WINDOW_HOURS = 24
RUNS_FINISHED = ("done", "failed")


def active_runs(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The cycles the server runs for the desks (`run_records`): the ones running now and the ones that finished in the
    last 24 hours. What the right panel shows under "In the background" (SPEC-013 13.5)."""
    from hokma.backoffice import runs

    database = _db(db)
    with database.read() as u:
        names = {d.id: d.name for d in u.desks.list(include_archived=True)}
    since = (now or datetime.now(UTC)) - timedelta(hours=RUNS_WINDOW_HOURS)

    def row(r: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": r["id"], "desk_id": r["desk_id"], "desk_name": names.get(r["desk_id"], ""), "kind": r["kind"],
                "started_at": r["started_at"]}

    finished = [r for status in RUNS_FINISHED for r in runs.list_runs(status=status, limit=200, db=database)
                if r["ended_at"] and from_db_time(r["ended_at"]) >= since]
    finished.sort(key=lambda r: (r["ended_at"], r["id"]), reverse=True)
    return {"running": [row(r) for r in runs.list_runs(status="running", limit=200, db=database)],
            "finished_24h": [{**{k: v for k, v in row(r).items() if k != "started_at"}, "status": r["status"],
                              "ended_at": r["ended_at"]} for r in finished]}


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

def _claude_text(owner: str, name: str, mandate: str | None, capital: int | None, currency: str) -> str:
    path = app_root().joinpath(*CLAUDE_TEMPLATE)
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        body = FALLBACK_CLAUDE
    values = {"desk_name": name, "owner_name": owner or "", "capital": f"{capital:,} {currency}" if capital is not None else t("desks.claude.no_capital"),
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


ACCOUNT_SUFFIX = " demo"                       # the demo account of a desk is called "{desk} demo"
ACCOUNT_NAME_MAX = 60                          # `broker_accounts.name`


def account_request(account: Any, capital: Any) -> tuple[str, int | None]:
    """What the caller asked for the money of a new desk (SPEC-025 delivery 2), checked before anything is written.

    `account` is `{"mode": "new", "initial_capital": n}` or `{"mode": "existing", "account_id": n}`. Without it the desk has no
    account (SPEC-027: a desk is born with its agents and no money), and `capital`, if given, is the optional capital of the desk;
    nobody writes a default. Returns `("new", capital)`, `("existing", id)` or `("none", capital or None)`."""
    if account is None:
        return "none", None if capital is None else _capital(capital)
    if not isinstance(account, dict):
        raise DeskError("bad_account", 400)
    mode = account.get("mode")
    if mode == "new":
        return "new", _capital(account.get("initial_capital"))
    if mode == "existing":
        found = account.get("account_id")
        if not isinstance(found, int) or isinstance(found, bool):
            raise DeskError("bad_account", 400)
        return "existing", found
    raise DeskError("bad_account", 400)


def _demo_account(u, desk_name: str, capital: int):
    """The demo account of a new desk, in the internal broker (the one of kind `sim`). A name that is taken gets a number."""
    sim = next((b for b in u.broker.list_brokers() if b.kind == "sim"), None)
    if sim is None:
        raise DeskError("no_demo_broker", 409)
    base = f"{desk_name[:ACCOUNT_NAME_MAX - len(ACCOUNT_SUFFIX) - 4].strip()}{ACCOUNT_SUFFIX}"
    name, n = base, 1
    while u.broker.find_account_by_name(sim.id, name) is not None:
        n += 1
        name = f"{base} {n}"
    return u.broker.create_account(sim.id, name, capital)


def money_of_new_desk(u, plan: tuple[str, int | None], desk_name: str) -> tuple[int | None, Any]:
    """Inside the transaction of the desk: the capital of the desk and its account (None when no account was asked).
    The capital of the desk is the `initial_capital` of the account. A failure raises and the caller's transaction undoes
    everything: there is no desk without its account and no account without its desk."""
    kind, value = plan
    if kind == "none":
        return value, None
    if kind == "new":
        try:
            return value, _demo_account(u, desk_name, value)
        except ValidationError as exc:
            raise DeskError("bad_capital", 400) from exc
    try:
        account = u.broker.get_account(value)
    except NotFound as exc:
        raise DeskError("account_not_found", 404) from exc
    if account.status != "active":
        raise DeskError("account_not_found", 404)
    return _capital(int(account.initial_capital)), account


def link_new_desk_account(u, desk_id: int, account: Any) -> None:
    if account is None:
        return
    broker_desk_accounts.link_account(u, desk_id, account)       # an account may serve other desks too (SPEC-027); the first one is the principal strategy's


def create_desk(name: str, capital: Any = None, *, account: Any = None, focus: str | None = None, template: str = "standard",
                objective_pct: Any = None, mandate: str | None = None, assets: Any = None,
                token_budget: Any = None, db: Database | None = None) -> dict[str, Any]:
    """Create a desk with the agents of `template`. Each agent keeps its default model for its role.

    `token_budget` None means no weekly cap of its own; the suggested figure of the template is data for the screen
    (`list_templates`), not a default applied here.
    """
    database = _db(db)
    clean_name = _name(name)
    plan = account_request(account, capital)
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
                clean_capital, desk_account = money_of_new_desk(u, plan, clean_name)      # the capital comes from the account
                desk = u.desks.create(project.id, clean_name, capital=clean_capital, focus=clean_focus,
                                      template=chosen.key, objective_pct=clean_objective, mandate=clean_mandate,
                                      token_budget=clean_budget)
                link_new_desk_account(u, desk.id, desk_account)
                u.broker.set_limits(desk.id, assets_limits(bool(clean_assets)))        # no rules: only the fixed guards, and the list of assets as a NOTICE if it has one
                for member in chosen.members:
                    u.desks.add_agent(desk.id, member.agent_slug, member.role, member.model)
                u.desks.replace_assets(desk.id, clean_assets)
                given = connections_for_new_desk(u, desk.id)
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
    out["connections"] = connections_report(given)
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

def _close_terminals_in(folder: Path) -> None:
    """Bug 1208: an open integrated terminal with its folder inside the desk folder keeps it in use on Windows and
    the move fails. Stop those shells first (synchronous kill, the same one the app uses on exit).
    Known and accepted (Vera r1, C2): the terminals are closed BEFORE the move, so if the move then fails (folder in
    use by something else) they stay closed; the owner just opens a new one. Closing after a failed move is not an
    option: the open shell is what makes the move fail."""
    from hokma.terminal.session import registry

    try:
        root = os.path.normcase(os.path.realpath(folder))
        for session in list(registry.sessions):
            cwd = os.path.normcase(os.path.realpath(session.cwd))
            if cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep):
                session.closed = True
                session.pty.kill_now()
                registry.discard(session)
    except Exception as exc:       # noqa: BLE001  (closing terminals is best effort; the move reports the real cause)
        log.warning("could not close terminals of the desk folder: %s", type(exc).__name__)


def _log_delete_failure(exc: BaseException, desk_id: int) -> str:
    """Write why the delete failed to engine.log (code and error kind, no paths) and return the DeskError code."""
    from hokma.engine import errlog

    code = getattr(exc, "code", None) or "db_failed"
    cause = exc.__cause__ if isinstance(exc, trash.TrashError) else exc
    detail = type(cause).__name__ if cause is not None else ""
    if isinstance(cause, OSError):
        detail += f" errno={cause.errno} winerror={getattr(cause, 'winerror', None)}"
    in_use = code == "move_failed" and (isinstance(cause, PermissionError) or getattr(cause, "winerror", None) == 32)
    message = detail if code == "move_failed" or not isinstance(exc, trash.TrashError) else exc.message
    errlog.log(f"desk {desk_id} could not be deleted ({code}): {message}", scope="desks")
    if in_use:
        return "delete_in_use"
    if isinstance(exc, trash.TrashError):
        return "delete_move_failed" if code == "move_failed" else "delete_failed"
    return "delete_db_failed"


def delete_desk(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """Delete a desk: its folder goes to the Trash (restorable); the desk row, its team assignments, assets and
    connections and its project row are removed. Conversations stay in the history (their project becomes none) and
    the student's agents are not touched. The folder moves first: if the database step fails the folder is put back."""
    from hokma.kit.settings import protected_paths

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
            _close_terminals_in(folder)
            try:
                moved = trash.move([str(folder)], t("desks.trash_reason", name=name), db=database,
                                   roots=[str(root)], protect=[str(p) for p in protected_paths()], cwd=str(root),
                                   project_id=None)
            except trash.TrashError as exc:
                raise DeskError(_log_delete_failure(exc, desk_id), 500) from exc
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
            _log_delete_failure(exc, desk_id)
            raise DeskError("delete_db_failed", 500) from exc
    return {"id": desk_id, "name": name, "trash_id": item_id}


# ---- update ----------------------------------------------------------------------------------

def update_desk(desk_id: int, changes: dict[str, Any], db: Database | None = None, *,
                lower_only: bool = False) -> dict[str, Any]:
    """`changes` holds only the fields to change: name, focus, capital, objective_pct, mandate, token_budget,
    status. Archiving is `status='archived'`; deleting is `delete_desk`.

    `lower_only` is what the tools of the agents use (SPEC-015 bug 1220): the capital may stay or go down, never up, compared
    with the one saved INSIDE the same transaction, so two calls that race cannot add up to a raise. A desk that is paused or
    archived does not become active either: that is the owner's. Raising is the owner's, in the app or through the tool that
    always asks."""
    database = _db(db)
    values: dict[str, Any] = {}
    if "name" in changes:
        values["name"] = _name(changes["name"])
    if "focus" in changes:
        values["focus"] = _text(changes["focus"], 120, "bad_focus")
    if "capital" in changes:
        values["capital"] = None if changes["capital"] is None else _capital(changes["capital"])       # None: the desk has no capital of its own
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
            current = _desk(u, desk_id)
            if lower_only and (_raises_capital(current.capital, values.get("capital", current.capital))
                               or (values.get("status", current.status) == "active" and current.status != "active")):
                raise broker_service.owner_only("raising the capital or reactivating a desk")
            u.desks.update(desk_id, **{k: values.get(k, UNSET) for k in (
                "name", "focus", "capital", "objective_pct", "mandate", "token_budget", "status")})
            return _view(u, u.desks.get(desk_id), detail=True)
    except (IntegrityViolation, ValidationError) as exc:
        raise DeskError("db_failed", 400) from exc


# ---- panels of the screen (SPEC-025, delivery 3) ---------------------------------------------------------------------------------

def _save_panels(desk_id: int, change, db: Database | None) -> dict[str, Any]:
    """Read the order, change it and save it in one transaction, so two quick changes (the owner's and the chief's) never lose each other."""
    try:
        with _db(db).write() as u:
            raw = _desk(u, desk_id).layout
            u.desks.set_layout(desk_id, panels.dump(change(panels.resolve(raw)), panels.saved(raw)))     # the saved strategies travel with the order
            return _view(u, u.desks.get(desk_id), detail=True)
    except (IntegrityViolation, ValidationError) as exc:
        raise DeskError("db_failed", 400) from exc


def _check_analysis(desk_id: int, db: Database | None) -> None:
    """The Analysis panel is only for a desk that has the cycle of agents (SPEC-028, delivery 4): a desk without it is refused with the reason."""
    from hokma.desks.swo import analysis

    analysis.require_available(desk_id, _db(db))


def saved_strategies(desk_id: int, db: Database | None = None) -> dict[str, dict[str, Any]]:
    """The strategies saved from the Option detail panel, by strategy_key (SPEC-028, 3339-B). They live in the layout of the desk: no table."""
    with _db(db).read() as u:
        return panels.saved(_desk(u, desk_id).layout)


def save_strategy(desk_id: int, key: str, record: dict[str, Any], limit: int, db: Database | None = None) -> None:
    """Keep `record` under `key` (a key that exists is replaced). A desk holds at most `limit` strategies: 409 saved_strategies_full."""
    try:
        with _db(db).write() as u:
            raw = _desk(u, desk_id).layout
            items = panels.saved(raw)
            if key not in items and len(items) >= limit:
                raise DeskError("saved_strategies_full", 409)
            items[key] = record
            u.desks.set_layout(desk_id, panels.dump(panels.resolve(raw), items))
    except (IntegrityViolation, ValidationError) as exc:
        raise DeskError("db_failed", 400) from exc


def delete_saved_strategy(desk_id: int, key: str, db: Database | None = None) -> None:
    try:
        with _db(db).write() as u:
            raw = _desk(u, desk_id).layout
            items = panels.saved(raw)
            if key not in items:
                raise DeskError("saved_strategy_not_found", 404)
            del items[key]
            u.desks.set_layout(desk_id, panels.dump(panels.resolve(raw), items))
    except (IntegrityViolation, ValidationError) as exc:
        raise DeskError("db_failed", 400) from exc


def set_panels(desk_id: int, order: Any, db: Database | None = None) -> dict[str, Any]:
    """The whole order of the panels, as the owner left it after dragging or moving with the keyboard. The three fixed ones must be in it."""
    wanted = panels.check_order(order)
    if "analysis" in wanted:
        _check_analysis(desk_id, db)
    return _save_panels(desk_id, lambda _current: wanted, db)


def add_panel(desk_id: int, panel: Any, db: Database | None = None) -> dict[str, Any]:
    """Add a panel before the fixed ones. The chief calls this too: a panel moves no money and no rule, so it is not asked."""
    if str(panel) == "analysis":
        _check_analysis(desk_id, db)
    return _save_panels(desk_id, lambda current: panels.with_added(current, str(panel)), db)


def remove_panel(desk_id: int, panel: Any, db: Database | None = None) -> dict[str, Any]:
    """Take a panel off the screen. rules, agents and config cannot be taken off. Nothing but the screen changes."""
    return _save_panels(desk_id, lambda current: panels.with_removed(current, str(panel)), db)


def _raises_capital(current: int | None, wanted: int | None) -> bool:
    """Whether a change loosens the money of the desk: a bigger number, or taking the number away from a desk that has one.
    Giving a number to a desk without one is a limit, so it is not."""
    if current is None:
        return False
    return wanted is None or wanted > current


def archive_desk(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    return update_desk(desk_id, {"status": "archived"}, db)


ASSETS_NOTICE = {"mode": "notice", "timeout_min": 30}


def assets_limits(has_assets: bool) -> dict[str, Any]:
    """The limits a new desk is born with (SPEC-026, section 8). No rule is imposed: the list of assets, when the desk has one, is a NOTICE, so a
    candidate outside the list waits for the owner and is never refused. The guard runs because it is in `guards`; the mode decides what it does."""
    if not has_assets:
        return {"guards": []}
    return {"guards": ["whitelist"], "rule_modes": {"assets_only": dict(ASSETS_NOTICE)}}


def keep_assets_rule(u, desk_id: int, has_assets: bool) -> None:
    """A desk with a `guards` list and a list of assets applies the list: `whitelist` is added to `guards` so the guard can run, and the rule of the
    assets is a NOTICE unless the owner already chose a mode for it. A desk without the list (guards None) already runs every guard and is
    left alone. The system never makes the list a restriction by itself."""
    config = dict(u.broker.get_limits(desk_id))
    guards = config.get("guards")
    if not has_assets or guards is None:
        return
    modes = dict(config.get("rule_modes") or {})
    if "whitelist" in guards and "assets_only" in modes:
        return
    modes.setdefault("assets_only", dict(ASSETS_NOTICE))
    u.broker.set_limits(desk_id, {**config, "guards": list(dict.fromkeys([*guards, "whitelist"])), "rule_modes": modes})


def set_assets(desk_id: int, symbols: Any, db: Database | None = None, *, keep_assets_only: bool = False) -> list[dict[str, Any]]:
    """Replace the list (at most 20). Checking each symbol against the market catalog is the API layer's job.

    `keep_assets_only` is what the tools of the agents use (bug 1220): while the limit `assets_only` of 18.5 is on, the symbols
    of the desk ARE that limit, so a symbol that is not in the list saved now is a loosening and is the owner's."""
    clean = _assets(symbols)
    with _db(db).write() as u:
        _desk(u, desk_id)
        if keep_assets_only and u.broker.get_limits(desk_id).get("assets_only") is True:
            have = {a.symbol.upper() for a in u.desks.list_assets(desk_id)}
            if any(symbol.upper() not in have for symbol, _note in clean):
                raise broker_service.owner_only("adding an asset while the assets_only limit is on")
        rows = u.desks.replace_assets(desk_id, clean)
        keep_assets_rule(u, desk_id, bool(rows))
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


def connections_for_new_desk(u, desk_id: int) -> list[str]:
    """A desk is born with every provider the owner already has connected turned on (SPEC-015 4.3 rule 5 for desks made later),
    in the order of Connections for the bars. Returns their ids; empty when nothing is connected: the desk then only has the
    Yahoo fallback and the caller says so. Runs inside the write transaction of the creation, so no desk is born half done."""
    service = _market_service()
    if not getattr(service, "loaded", True):
        service.load()
    added = [pid for pid in service.catalog.providers if service.is_added(pid)]
    rank = {pid: i for i, pid in enumerate(service.order("bars"))}
    added.sort(key=lambda pid: rank.get(pid, len(rank)))
    for pid in added:
        u.desks.set_connection(desk_id, pid, True)
    return added


def activate_my_connections(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """The button "Activar mis conexiones": turn on in this desk every provider the owner has connected that the desk has no row for.
    Idempotent. A row the desk already has, on or off, is the owner's explicit choice and is never touched. Returns what is on now:
    `activated` (rows made by this call), `providers` (every provider the desk uses, in the order of Connections for the bars) and
    `fallback_only` (nothing connected and nothing on: only the Yahoo fallback is left)."""
    service = _market_service()
    if not getattr(service, "loaded", True):
        service.load()
    added = [pid for pid in service.catalog.providers if service.is_added(pid)]
    rank = {pid: i for i, pid in enumerate(service.order("bars"))}
    added.sort(key=lambda pid: rank.get(pid, len(rank)))
    made: list[str] = []
    with _db(db).write() as u:
        _desk(u, desk_id)
        have = {c.provider_id for c in u.desks.list_connections(desk_id)}
        for pid in added:
            if pid not in have:
                u.desks.set_connection(desk_id, pid, True)
                made.append(pid)
        on = [c.provider_id for c in u.desks.list_connections(desk_id) if c.enabled]
    on.sort(key=lambda pid: rank.get(pid, len(rank)))
    return {"activated": made, "providers": on, "fallback_only": not on}


FALLBACK_NOTICE = "esta mesa solo tiene el respaldo (Yahoo); conecta Schwab o Finnhub en Conexiones"


def connections_report(provider_ids: list[str]) -> dict[str, Any]:
    """What the creation answers about the connections of the new desk: the providers it uses, or that only the fallback is left."""
    out: dict[str, Any] = {"providers": provider_ids, "fallback_only": not provider_ids}
    if not provider_ids:
        out["notice"] = FALLBACK_NOTICE
        out["link"] = "connections"                                  # the screen of the app where Schwab or Finnhub are connected
    return out


def enable_connection_everywhere(provider_id: str, db: Database | None = None) -> list[int]:
    """A provider was just added in Connections (SPEC-015 4.3 rule 5): turn it on in every desk that has no row for it.
    A desk that already has a row (on or off) keeps the owner's choice. Idempotent. Returns the desks that got a row."""
    if _market_service().catalog.get(provider_id) is None:
        raise DeskError("bad_connection", 404)
    created: list[int] = []
    with _db(db).write() as u:
        for desk in u.desks.list(include_archived=True):
            if not any(c.provider_id == provider_id for c in u.desks.list_connections(desk.id)):
                u.desks.set_connection(desk.id, provider_id, True)
                created.append(desk.id)
    return created


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

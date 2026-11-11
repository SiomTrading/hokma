"""Agents: list, model tier, on/off, create, and the startup reconcile between the database and the files."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hokma.agents import files as fl
from hokma.core.config import hokma_data_dir
from hokma.db import Database, DbError, NotFound, get_database

log = logging.getLogger("hokma.agents")

ORCHESTRATOR_SLUG = "hokma"
# The kit installs these on; the rest of the agents (and the sub-agents of Joel) follow the table in HOKMA.md.
BASE_KIT = frozenset({"hokma", "diego", "joel", "vera"})


_synced: dict[str, tuple] = {}


class AgentsError(Exception):
    """`code` is translated by the API layer; `status` is the HTTP status."""

    def __init__(self, code: str, status: int = 422, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code, self.status = code, status


def state_path() -> Path:
    return hokma_data_dir() / "agents-state.json"


def _initialized() -> set[str]:
    try:
        return set(json.loads(state_path().read_text(encoding="utf-8")).get("initialized", []))
    except (OSError, ValueError, AttributeError):
        return set()


def _save_initialized(slugs: set[str]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"initialized": sorted(slugs)}, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def _wrap(exc: DbError) -> AgentsError:
    if isinstance(exc, NotFound):
        return AgentsError("agent_not_found", 404, str(exc))
    return AgentsError("agent_invalid", 422, str(exc))


@dataclass
class ReconcileReport:
    activated: list[str] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)


class AgentService:
    def __init__(self, db: Database | None = None, files: fl.AgentFiles | None = None) -> None:
        self._db = db
        self.files = files or fl.AgentFiles()

    @property
    def db(self) -> Database:
        return self._db or get_database()

    # ------------------------------------------------------------------ reading

    def _item(self, agent) -> dict[str, Any]:
        try:
            role = self.files.role_of(agent.slug, agent.file_path)
        except fl.AgentFileError:
            role = None
        orchestrator = role == "orchestrator" or (role is None and agent.slug == ORCHESTRATOR_SLUG)
        return {"slug": agent.slug, "name": agent.display_name, "description": agent.description or "",
                "tier": agent.model_tier, "model": fl.TIER_MODEL[agent.model_tier], "status": agent.status,
                "active": agent.status == "active", "is_custom": agent.is_custom,
                "is_orchestrator": orchestrator, "enabled_session": agent.enabled_session, "role": role}

    def list(self) -> dict[str, Any]:
        try:
            self.sync_folder()
        except Exception:
            log.exception("agents folder not synced")
        with self.db.read() as u:
            agents = u.agents.list()
            active, total = u.agents.count("active"), u.agents.count()
        items = [self._item(a) for a in agents]
        orchestrator = next((i for i in items if i["is_orchestrator"]), None)
        rest = [i for i in items if i is not orchestrator]
        rest.sort(key=lambda i: (not i["active"], i["is_custom"], i["name"].lower()))
        return {"orchestrator": orchestrator, "agents": rest, "active": active, "total": total}

    def summary(self) -> dict[str, int]:
        with self.db.read() as u:
            return {"active": u.agents.count("active"), "total": u.agents.count()}

    def _get(self, slug: str):
        with self.db.read() as u:
            try:
                return u.agents.get(slug)
            except DbError as exc:
                raise _wrap(exc) from exc

    # ------------------------------------------------------------------ changes

    def set_model(self, slug: str, model: str) -> dict[str, Any]:
        if model not in fl.MODELS:
            raise AgentsError("model_invalid", 422, f"model must be one of {', '.join(fl.MODELS)}")
        agent = self._get(slug)
        try:
            self.files.apply(slug, agent.file_path, active=agent.status == "active", model=model)
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        try:
            with self.db.write() as u:
                updated = u.agents.set_model_tier(slug, fl.MODEL_TIER[model])
        except DbError as exc:
            raise _wrap(exc) from exc
        return self._item(updated)

    def set_status(self, slug: str, status: str) -> dict[str, Any]:
        if status not in ("active", "off"):
            raise AgentsError("status_invalid", 422)
        agent = self._get(slug)
        item = self._item(agent)
        if status == "off" and item["is_orchestrator"]:
            raise AgentsError("orchestrator_required", 409)
        try:
            self.files.apply(slug, agent.file_path, active=status == "active", model=fl.TIER_MODEL[agent.model_tier])
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        try:
            with self.db.write() as u:
                updated = u.agents.set_status(slug, status)
        except DbError as exc:
            raise _wrap(exc) from exc
        return self._item(updated)

    def create(self, name: str, description: str, model: str, instructions: str, tools: str | None = None,
               active: bool = True) -> dict[str, Any]:
        display = fl.clean_name(name)
        text = fl.clean_text(description, fl.MAX_DESCRIPTION)
        body = fl.clean_text(instructions, fl.MAX_INSTRUCTIONS)
        slug = fl.slugify(display)
        if not display or len(slug) < 2:
            raise AgentsError("name_invalid", 422)
        if not text:
            raise AgentsError("description_required", 422)
        if not body:
            raise AgentsError("instructions_required", 422)
        if model not in fl.MODELS:
            raise AgentsError("model_invalid", 422)
        rel = f"{fl.SOURCE_DIR}/{slug}.md"
        with self.db.read() as u:
            taken = u.agents.find(slug) is not None
        try:
            if taken or self.files.read_source(slug, rel) is not None:
                raise AgentsError("agent_exists", 409)
            self.files.write_source(slug, rel, fl.build_custom_agent(slug, display, text, model, body, tools))
            self.files.apply(slug, rel, active=active, model=model)
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        try:
            with self.db.write() as u:
                u.agents.create_custom(slug, display, description=text, model_tier=fl.MODEL_TIER[model], file_path=rel)
                created = u.agents.set_status(slug, "active" if active else "off")
        except DbError as exc:
            for rel_dir in (fl.LOADED_DIR, fl.OFF_DIR):                  # no row: do not leave a loadable file behind
                try:
                    self.files._remove(f"{rel_dir}/{slug}.md")
                except fl.AgentFileError:
                    pass
            raise _wrap(exc) from exc
        state = _initialized()
        state.add(slug)
        _save_initialized(state)
        return self._item(created)

    def update(self, slug: str, *, description: str | None = None, model: str | None = None,
               instructions: str | None = None, tools: str | None = None, active: bool | None = None) -> dict[str, Any]:
        """Edit an agent the owner made (a field not given stays as it is). The kit's own agents change only their
        model and their switch, from the panel."""
        agent = self._get(slug)
        if not agent.is_custom:
            raise AgentsError("agent_not_custom", 409, "only agents you created can be edited")
        try:
            current = fl.read_custom_agent(self.files.read_source(slug, agent.file_path) or "")
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        if current is None:
            raise AgentsError("agent_file_invalid", 409)
        text = fl.clean_text(description, fl.MAX_DESCRIPTION) if description is not None else current["description"]
        body = fl.clean_text(instructions, fl.MAX_INSTRUCTIONS) if instructions is not None else current["instructions"]
        chosen = model if model is not None else current["model"]
        if chosen not in fl.MODELS:
            raise AgentsError("model_invalid", 422)
        if not text:
            raise AgentsError("description_required", 422)
        if not body:
            raise AgentsError("instructions_required", 422)
        status = agent.status if active is None else ("active" if active else "off")
        rel = self.files.source_rel(slug, agent.file_path)
        keep_tools = fl.clean_tools(tools) if tools else (current["tools"] or fl.CUSTOM_TOOLS)
        try:
            self.files.write_source(slug, rel, fl.build_custom_agent(
                slug, current["display_name"] or agent.display_name, text, chosen, body, keep_tools))
            self.files.apply(slug, rel, active=status == "active", model=chosen)
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        try:
            with self.db.write() as u:
                u.agents.update(slug, description=text)
                u.agents.set_model_tier(slug, fl.MODEL_TIER[chosen])
                updated = u.agents.set_status(slug, status)
        except DbError as exc:
            raise _wrap(exc) from exc
        return self._item(updated)

    def save_skill(self, name: str, description: str, instructions: str) -> dict[str, Any]:
        """Create or replace `.claude/skills/<slug>/SKILL.md` in the working folder (what Claude Code loads)."""
        display = fl.clean_name(name)
        slug = fl.slugify(display)
        text = fl.clean_text(description, fl.MAX_DESCRIPTION)
        body = fl.clean_text(instructions, fl.MAX_SKILL)
        if not display or len(slug) < 2:
            raise AgentsError("name_invalid", 422)
        if not text:
            raise AgentsError("description_required", 422)
        if not body:
            raise AgentsError("instructions_required", 422)
        try:
            existed = self.files.read_skill(slug) is not None
            self.files.write_skill(slug, fl.build_skill(slug, text, body))
        except fl.AgentFileError as exc:
            raise AgentsError(exc.code, 409, str(exc)) from exc
        return {"slug": slug, "path": f"{fl.SKILLS_DIR}/{slug}/SKILL.md", "updated": existed}

    # ------------------------------------------------------------------ folder changes

    def folder_signature(self) -> tuple:
        try:
            root = self.files.root / fl.SOURCE_DIR
            return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in root.glob("*.md")))
        except OSError:
            return ()

    def sync_folder(self, *, force: bool = False) -> bool:
        """An agent written straight into `agents/` (by the chat, with the editor, by hand) shows up in the panel and
        starts on, like the ones made with the Create button. Runs only when the folder changed since the last run."""
        from hokma.db.seeds import seed_agents

        key = str(self.files.root)
        signature = self.folder_signature()
        if not force and _synced.get(key) == signature:
            return False
        with self.db.write() as u:
            seed_agents(u.session)
        self.reconcile()
        _synced[key] = signature
        return True

    # ------------------------------------------------------------------ startup

    def reconcile(self) -> ReconcileReport:
        """Give new kit agents their first state, then make the files match the database.
        The owner's choices win: an agent is given its first state once, ever (agents-state.json)."""
        report = ReconcileReport()
        seen = _initialized()
        with self.db.read() as u:
            agents = u.agents.list()
        for agent in agents:
            try:
                role = self.files.role_of(agent.slug, agent.file_path)
                status = agent.status
                if agent.slug not in seen:
                    status = "active" if (agent.is_custom or agent.slug in BASE_KIT or role in ("orchestrator", "sub-agent")) \
                        else "off"
                if (role == "orchestrator" or agent.slug == ORCHESTRATOR_SLUG) and status != "active":
                    status = "active"
                if status != agent.status:
                    with self.db.write() as u:
                        u.agents.set_status(agent.slug, status)
                    if status == "active":
                        report.activated.append(agent.slug)
                self.files.apply(agent.slug, agent.file_path, active=status == "active",
                                 model=fl.TIER_MODEL[agent.model_tier])
                seen.add(agent.slug)
            except (fl.AgentFileError, DbError, OSError) as exc:
                report.problems[agent.slug] = str(exc)
                log.warning("agent %s not reconciled: %s", agent.slug, exc)
        _save_initialized(seen)
        return report

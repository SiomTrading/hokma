"""Create a stock options swing (SWO) desk with its plantel (SPEC-019: B6).

One call makes the whole desk or nothing: the project and the desk row (template `custom`), one `desk_agents` row and one
`desk_seats` row per seat of the plantel, the line profile, the autonomy level 1, the typed mandate, the folder, the
agent files and the skills of the team. What this call wrote on disk is removed if any step fails.

One team per desk (decision 1088): every seat gets its own agent, named after the seat and the desk
(`market-analyst-12`), so two desks never share an agent, its model or its files. The agent file and the skills of the
team come from the kit; a skill enters a seat only because the file of that seat declares it, and a seat file that does not
carry the SWO line is refused, so a skill of another line never reaches the prompt of a SWO agent.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hokma.agents import files as agent_files
from hokma.db import Database, DbError
from hokma.db.repos.projects import slugify
from hokma.db.timeutil import utcnow
from hokma.desks import service
from hokma.desks.agents.manager import Mandate, analyst_plan, load_mandate, parse_mandate
from hokma.desks.core.skills import SkillError, parse_skill, split_frontmatter
from hokma.desks.core.spec_loader import SpecError
from hokma.desks.errors import DeskError
from hokma.desks.plantel import SWO_VARIANTS, Seat, seats_for
from hokma.docs import diskfolders
from hokma.i18n import t
from hokma.kit import safefs
from hokma.kit.integrity import app_root
from hokma.projects import gitops
from hokma.projects import service as projects
from hokma.projects.errors import ProjectError

LINE = "SWO"
TEAM_DIR = "team"
MAX_SKILLS_PER_SEAT = 12
MAX_FILE_BYTES = 200_000
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def default_kit_root() -> Path:
    """The desk part of the kit in this install."""
    return app_root() / "kit" / "template" / "desk"


@dataclass(frozen=True)
class _Member:
    seat: Seat
    text: str                                   # the agent file of the seat, as the kit has it
    skills: tuple[tuple[str, str], ...]         # (name, text) of each skill the seat declares


def _read_kit_file(path: Path) -> str:
    """One kit file as text. A missing, linked, oversized or unreadable file is an agent that is not installed."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise DeskError("agent_missing", 409)
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DeskError("agent_missing", 409) from exc


def _load_member(seat: Seat, kit_root: Path) -> _Member:
    text = _read_kit_file(kit_root / "agents" / f"{seat.slug}.md")
    try:
        meta, _ = split_frontmatter(text, seat.kit_file)
    except SkillError as exc:
        raise DeskError("agent_missing", 409) from exc
    lines = meta.get("lines")
    if lines is not None and (not isinstance(lines, list) or LINE not in lines):
        raise DeskError("agent_missing", 409)      # the file of this seat is not written for a SWO desk
    names = meta.get("skills") or []
    if not isinstance(names, list) or len(names) > MAX_SKILLS_PER_SEAT or len(set(map(str, names))) != len(names):
        raise DeskError("agent_missing", 409)
    skills: list[tuple[str, str]] = []
    for name in names:
        if not isinstance(name, str) or not _SKILL_NAME.match(name):
            raise DeskError("agent_missing", 409)
        body = _read_kit_file(kit_root / "skills" / name / "SKILL.md")
        try:
            skill = parse_skill(body, f"{name}/SKILL.md")
        except SkillError as exc:
            raise DeskError("agent_missing", 409) from exc
        if skill.name != name:
            raise DeskError("agent_missing", 409)
        skills.append((name, body))
    return _Member(seat, text, tuple(skills))


def desk_agent_slug(seat: Seat, desk_id: int) -> str:
    """The agent of a seat in one desk. Unique per desk, so no two desks share an agent."""
    return f"{seat.slug}-{desk_id}"


def _agent_text(member: _Member, slug: str) -> str:
    seat = member.seat
    text = member.text
    for key, value in (("name", slug), ("slug", slug), ("display_name", seat.name), ("model", seat.default_model),
                       ("hokma-tier", agent_files.MODEL_TIER[seat.default_model])):
        text = agent_files.set_frontmatter_value(text, key, value)
    return text


def _mandate(variant: str, mandate: Mandate | Mapping[str, Any] | None, kit_root: Path) -> Mandate:
    try:
        if mandate is None:
            chosen = load_mandate(kit_root / "mandates" / f"swo-{variant}.yaml")
        elif isinstance(mandate, Mandate):
            chosen = parse_mandate(mandate.model_dump(mode="json"))
        else:
            chosen = parse_mandate(mandate)
    except SpecError as exc:
        raise DeskError("bad_mandate", 400) from exc
    if chosen.line != LINE or (chosen.variant or "traditional") != variant:
        raise DeskError("bad_mandate", 400)
    if len(chosen.summary()) > 4000:
        raise DeskError("bad_mandate", 400)
    return chosen


def create_swo_desk(name: str, capital: Any = None, *, account: Any = None, variant: str = "traditional", mandate: Mandate | Mapping[str, Any] | None = None,
                    assets: Any = None, objective_pct: Any = None, token_budget: Any = None, owner_approved: bool = False,
                    kit_root: Path | None = None, db: Database | None = None) -> dict[str, Any]:
    """Create a SWO desk with its plantel. `variant` is `traditional` or `pead`. Without a mandate the kit example of the
    variant is used. `owner_approved` marks the mandate as the owner's (set only by the owner's own request).

    Everything is checked before anything is written; a seat whose kit file is missing refuses the whole call."""
    database = service._db(db)
    if variant not in SWO_VARIANTS:
        raise DeskError("bad_template", 400)
    clean_name = service._name(name)
    money = service.account_request(account, capital)
    clean_objective = service._objective(objective_pct)
    clean_budget = service._budget(token_budget)
    service.check_symbols(assets if assets is not None else [])
    clean_assets = service._assets(assets if assets is not None else [])
    root_kit = kit_root if kit_root is not None else default_kit_root()
    chosen = _mandate(variant, mandate, root_kit)
    plan = analyst_plan(chosen)                       # a plan that does not hold fails here, before any write
    seats = seats_for(LINE, variant)
    members = [_load_member(seat, root_kit) for seat in seats]
    if sum(1 for m in members if m.seat.desk_role == "chief") != 1 or not plan:
        raise DeskError("bad_template", 400)
    with database.read() as u:
        owner = u.profile.get_or_default().owner_name
    mandate_text = chosen.summary()

    files = agent_files.AgentFiles()
    sources: list[str] = []
    with projects._LOCK:
        projects_root = projects.projects_root(database)
        slug = projects._unique_slug(database, projects_root, slugify(clean_name))
        try:
            folder = projects._make_folder(projects_root, slug)
        except ProjectError as exc:
            raise DeskError("folder_failed", exc.status) from exc
        written: list[str] = []
        try:
            with database.write() as u:
                project = u.projects.create(clean_name, str(folder), slug=slug, kind="strategy", description=None)
                clean_capital, desk_account = service.money_of_new_desk(u, money, clean_name)      # the capital comes from the account
                desk = u.desks.create(project.id, clean_name, capital=clean_capital, focus=None, template="custom",
                                      objective_pct=clean_objective, mandate=mandate_text, token_budget=clean_budget)
                service.link_new_desk_account(u, desk.id, desk_account)
                now = utcnow()
                u.broker.set_limits(desk.id, service.assets_limits(bool(clean_assets)))        # no rules: only the fixed guards, and the list of assets as a NOTICE if it has one
                u.session.insert_row("desk_profile", {"desk_id": desk.id, "line": LINE, "variant": variant,
                                                      "benchmark": None, "updated_at": now})
                u.session.insert_row("desk_autonomy", {"desk_id": desk.id, "level": 1, "rules_json": None,
                                                       "approved_by": None, "approved_at": None, "updated_at": now})
                u.session.insert_row("desk_mandates", {"desk_id": desk.id, "spec_json": chosen.to_json(), "version": 1,
                                                       "approved_by": "owner" if owner_approved else None, "updated_at": now})
                for member in members:
                    seat = member.seat
                    agent_slug = desk_agent_slug(seat, desk.id)
                    text = _agent_text(member, agent_slug)
                    rel = f"agents/{agent_slug}.md"
                    description = agent_files.frontmatter_value(text, "description") or f"{seat.name}, seat {seat.code}"
                    u.agents.upsert_kit(agent_slug, seat.name, description[:500], agent_files.MODEL_TIER[seat.default_model],
                                        rel, is_custom=True)
                    files.write_source(agent_slug, rel, text)
                    sources.append(agent_slug)
                    u.desks.add_agent(desk.id, agent_slug, seat.desk_role, seat.default_model)
                    u.session.insert_row("desk_seats", {"desk_id": desk.id, "seat_code": seat.code, "agent_slug": agent_slug,
                                                        "line": LINE, "variant": variant, "active": 1, "added_at": now})
                    safefs.write_atomic(folder, f"{TEAM_DIR}/agents/{seat.slug}.md", text.encode("utf-8"))
                    written += [TEAM_DIR, f"{TEAM_DIR}/agents", f"{TEAM_DIR}/agents/{seat.slug}.md"]
                    for skill_name, body in member.skills:
                        safefs.write_atomic(folder, f"{TEAM_DIR}/skills/{skill_name}/SKILL.md", body.encode("utf-8"))
                        written += [f"{TEAM_DIR}/skills", f"{TEAM_DIR}/skills/{skill_name}", f"{TEAM_DIR}/skills/{skill_name}/SKILL.md"]
                u.desks.replace_assets(desk.id, clean_assets)
                given = service.connections_for_new_desk(u, desk.id)
                for sub in projects.SUBFOLDERS:
                    safefs.write_atomic(folder, f"{sub}/.gitkeep", b"")
                    written += [sub, f"{sub}/.gitkeep"]
                claude = service._claude_text(owner, clean_name, mandate_text, clean_capital, desk.currency)
                safefs.write_atomic(folder, service.CLAUDE_FILE, claude.encode("utf-8"))
                written.append(service.CLAUDE_FILE)
        except (DbError, OSError, agent_files.AgentFileError, safefs.UnsafePath) as exc:
            _undo(folder, written, files, sources)
            raise DeskError("db_failed" if isinstance(exc, DbError) else "folder_failed", 500) from exc
        except BaseException:
            _undo(folder, written, files, sources)
            raise
    service._switch_on([desk_agent_slug(m.seat, desk.id) for m in members], database)
    git = gitops.init_repo(folder, t("projects.git.created"))
    diskfolders.ensure_own_folder(project.id, database)
    out = service.get_desk(desk.id, database)
    out["git"] = {"ok": git.ok, "reason": git.reason}
    out["connections"] = service.connections_report(given)
    out["line"], out["variant"] = LINE, variant
    out["seats"] = [{"seat_code": m.seat.code, "name": m.seat.name, "agent_slug": desk_agent_slug(m.seat, desk.id)} for m in members]
    out["analysts"] = [step.key for step in plan if step.enabled]
    return out


def _undo(folder: Path, written: list[str], files: agent_files.AgentFiles, sources: list[str]) -> None:
    """Remove only what this call put on disk: the registered agent files, then the folder files deepest first."""
    for agent_slug in sources:
        try:
            os.unlink(files.root / "agents" / f"{agent_slug}.md")
        except OSError:
            pass
    seen: list[str] = []
    for rel in written:
        if rel not in seen:
            seen.append(rel)
    service._undo(folder, sorted(seen, key=lambda r: (r.count("/"), r)))

"""Idempotent seeds, run by migrate(): agents from the kit files and the profile from owner.json."""
from __future__ import annotations

import re
from pathlib import Path

from hokma.agents.files import AgentFileError, frontmatter_value, set_frontmatter_value
from hokma.core.config import hokma_home
from hokma.db.backend import Session
from hokma.db.errors import DbError
from hokma.db.repos.agents import AgentsRepo, check_slug
from hokma.db.repos.profile import ProfileRepo

_TIER = {"haiku": "fast", "fast": "fast", "sonnet": "balanced", "balanced": "balanced",
         "opus": "deep", "deep": "deep"}
_MARKER = re.compile(r"\{\{(owner_name|fund_name)\}\}")
MAX_DESCRIPTION = 1000


def parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level `key: value` pairs of a `---` frontmatter block; folded (`>`) and literal (`|`) blocks are joined."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    state: dict = {"key": None, "block": [], "folded": False}

    def flush() -> None:
        if state["key"] is not None and state["folded"]:
            data[state["key"]] = " ".join(part.strip() for part in state["block"] if part.strip())
        state["key"], state["block"] = None, []

    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match and not line.startswith((" ", "\t")):
            flush()
            key, value = match.group(1), match.group(2).strip()
            state["key"] = key
            state["folded"] = value in (">", "|", ">-", "|-")
            if not state["folded"]:
                data[key] = value.strip("'\"")
        elif state["folded"] and state["key"] is not None:
            state["block"].append(line)
    flush()
    return data


def _substitute(text: str) -> str:
    from hokma.kit import owner

    values = owner.marker_values()
    return _MARKER.sub(lambda m: values[m.group(1)], text)


# Words of a slug that are acronyms and keep their capitals in the name the owner sees ("joel-mcp-builder" is "Joel MCP Builder").
ACRONYMS = frozenset({"mcp", "qa", "api", "ui", "ux", "ai", "cli", "ci", "cd", "db", "sql", "seo", "css", "html", "pdf",
                      "sdk", "url", "csv", "json", "yaml", "http", "oauth", "rag", "llm", "ml", "etl", "ide", "pr", "id"})


def display_name_from_slug(slug: str) -> str:
    return " ".join(p.upper() if p in ACRONYMS else p.capitalize() for p in slug.split("-"))


def _read_agents(directory: Path) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for file in sorted(directory.glob("*.md")):
        if file.is_symlink() or not file.is_file():
            continue
        try:
            meta = parse_frontmatter(_substitute(file.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
        slug = (meta.get("slug") or meta.get("name") or "").strip().lower()
        try:
            check_slug(slug)
        except DbError:
            continue
        found[slug] = {
            "display_name": meta.get("display_name") or display_name_from_slug(slug),
            "description": (meta.get("description") or "")[:MAX_DESCRIPTION] or None,
            "model_tier": _TIER.get((meta.get("model") or "").strip().lower(), "balanced"),
            "file": file.name,
        }
    return found


def kit_agents_dir() -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "kit" / "template" / "agents"


def creator_services() -> tuple[str, ...]:
    from hokma.kit.hokma_tools import CREATOR_SERVICES

    return CREATOR_SERVICES


def _service_agents(kit_dir: Path, installed_dir: Path) -> dict[str, dict[str, str]]:
    """Ian, Sam and Ben (the strategy creator) live in `kit/template/service/agents/`, next to the other services, and not in
    `agents/`. They are agents of the fund all the same: the file is copied into the working folder's `agents/` (where every agent file
    lives) and they are read from there like any kit agent. Since 1.2.5 the copy of these three is renewed from the kit whenever it differs (an
    older copy kept the narrow tools of 1.2.1 for ever): only the model the owner chose in the panel is kept."""
    service_dir = kit_dir.parent / "service" / "agents"
    found = {s: m for s, m in _read_agents(service_dir).items() if s in creator_services()} if service_dir.is_dir() else {}
    for slug, meta in found.items() if installed_dir.is_dir() else ():                # no `agents/` yet: the kit itself is the source
        target = installed_dir / meta["file"]
        if target.is_symlink():
            continue
        try:
            wanted = _substitute((service_dir / meta["file"]).read_text(encoding="utf-8"))
            if target.exists():
                installed = target.read_text(encoding="utf-8")
                for key in ("model", "hokma-tier"):                 # the tier the owner picked in the panel stays
                    kept = frontmatter_value(installed, key)
                    if kept and frontmatter_value(wanted, key) != kept:
                        wanted = set_frontmatter_value(wanted, key, kept)
                if installed == wanted:
                    continue
            target.write_text(wanted, encoding="utf-8")
        except (OSError, UnicodeDecodeError, AgentFileError):
            pass                                                  # not written: it is read from the kit below and retried at the next start
    return found


def seed_agents(session: Session, *, workdir_agents: Path | None = None, kit_dir: Path | None = None) -> int:
    """Insert or refresh the agents of the kit. Returns how many rows were added or turned off."""
    kit_dir = kit_dir if kit_dir is not None else kit_agents_dir()
    installed_dir = workdir_agents if workdir_agents is not None else hokma_home() / "agents"
    kit = _read_agents(kit_dir) if kit_dir.is_dir() else {}
    services = _service_agents(kit_dir, installed_dir)
    kit = {**kit, **services} if (kit or services) else kit
    source = _read_agents(installed_dir) if installed_dir.is_dir() else kit
    source = {**source, **{s: m for s, m in services.items() if s not in source}}
    repo = AgentsRepo(session)
    changed = 0
    for slug, meta in source.items():
        existing = repo.find(slug)
        custom = bool(kit) and slug not in kit
        if existing is not None and existing.is_custom:
            custom = True                                         # an agent the owner created stays theirs
        repo.upsert_kit(slug, meta["display_name"], meta["description"], meta["model_tier"],
                        f"agents/{meta['file']}", is_custom=custom)
        changed += 0 if existing else 1
    changed += repo.mark_missing_off(source.keys())
    return changed


def seed_profile(session: Session) -> bool:
    """Create the profile from owner.json when there is no profile yet and a name was given."""
    from hokma.i18n import language
    from hokma.kit import owner

    repo = ProfileRepo(session)
    if repo.get() is not None:
        return False
    names = owner.load()
    if not names["owner_name"]:
        return False
    repo.save(owner_name=names["owner_name"], fund_name=names["fund_name"] or None, language=language(),
              workdir=str(hokma_home()))
    return True


SIM_BROKER = "Hokma"
LEGACY_SIM_BROKER = "Hokma Simulated"             # 1.0.8 and earlier: renamed once, its accounts and costs stay
SIM_CONFIG = {"commission": {"type": "bps", "value": 1.0, "min": 0.0}, "slippage_bps": 2.0}


def seed_broker(session: Session) -> bool:
    """The built-in broker, once. Its costs are editable later, so an existing row keeps its costs; a row that still
    has the name of 1.0.8 is only renamed."""
    from hokma.db.repos.broker import BrokerRepo

    repo = BrokerRepo(session)
    if not session.table_exists("brokers") or repo.find_broker(SIM_BROKER) is not None:   # no table: migrations cut short
        return False
    legacy = repo.find_broker(LEGACY_SIM_BROKER)
    if legacy is not None:
        repo.rename_broker(legacy.id, SIM_BROKER)
        return False
    repo.create_broker("sim", SIM_BROKER, SIM_CONFIG)
    return True


def run_seeds(session: Session) -> None:
    seed_agents(session)
    seed_profile(session)
    seed_broker(session)

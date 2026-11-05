"""Idempotent seeds, run by migrate(): agents from the kit files and the profile from owner.json."""
from __future__ import annotations

import re
from pathlib import Path

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
            "display_name": meta.get("display_name") or " ".join(p.capitalize() for p in slug.split("-")),
            "description": (meta.get("description") or "")[:MAX_DESCRIPTION] or None,
            "model_tier": _TIER.get((meta.get("model") or "").strip().lower(), "balanced"),
            "file": file.name,
        }
    return found


def kit_agents_dir() -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "kit" / "template" / "agents"


def seed_agents(session: Session, *, workdir_agents: Path | None = None, kit_dir: Path | None = None) -> int:
    """Insert or refresh the agents of the kit. Returns how many rows were added or turned off."""
    kit_dir = kit_dir if kit_dir is not None else kit_agents_dir()
    installed_dir = workdir_agents if workdir_agents is not None else hokma_home() / "agents"
    kit = _read_agents(kit_dir) if kit_dir.is_dir() else {}
    source = _read_agents(installed_dir) if installed_dir.is_dir() else kit
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


def run_seeds(session: Session) -> None:
    seed_agents(session)
    seed_profile(session)

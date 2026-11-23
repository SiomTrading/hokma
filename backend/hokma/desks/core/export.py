"""Export and import of the skills of a desk (SPEC-017: ED-039).

A skill is a folder with a `SKILL.md` (a short header with a name and a description, then the instructions) and, optionally,
supporting text files. Each engine reads skills from its own folder; `export_skills` writes them where the engine of the student
looks, and `import_skills` reads them back, so what goes out comes back identical. Nothing is ever deleted: a skill folder that
is already there is overwritten file by file.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
SKILL_FILE = "SKILL.md"
# Folder of skills per engine kind, relative to the root the engine works in.
LAYOUTS: dict[str, tuple[str, ...]] = {
    "claude_code": (".claude", "skills"),
    "codex": (".codex", "skills"),
    "gemini_cli": (".gemini", "skills"),
}


class ExportError(ValueError):
    """A skill or a folder cannot be exported or imported."""


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    files: Mapping[str, str] = field(default_factory=dict)       # supporting text files, relative path -> text

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.fullmatch(self.name) or len(self.name) > MAX_NAME:
            raise ExportError(f"a skill name is lowercase words joined by dashes, up to {MAX_NAME} characters: {self.name!r}")
        description = " ".join(str(self.description).split())
        if not description or len(description) > MAX_DESCRIPTION:
            raise ExportError(f"the description of {self.name} has 1 to {MAX_DESCRIPTION} characters")
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "body", _normalise(str(self.body)))
        clean: dict[str, str] = {}
        for rel, text in dict(self.files).items():
            clean[_check_relative(rel)] = _normalise(str(text))
        object.__setattr__(self, "files", clean)


def _check_relative(rel: str) -> str:
    parts = re.split(r"[\\/]", rel)
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel) or any(p in ("", ".", "..") for p in parts):
        raise ExportError(f"a supporting file must be a relative path inside the skill: {rel!r}")
    if rel == SKILL_FILE or parts[-1] == SKILL_FILE:
        raise ExportError("a supporting file cannot be called SKILL.md")
    return "/".join(parts)


def render_skill_md(skill: Skill) -> str:
    """The text of `SKILL.md`: a header with the name and description, a blank line, the instructions."""
    return f"---\nname: {skill.name}\ndescription: {json.dumps(skill.description, ensure_ascii=False)}\n---\n\n{skill.body}\n"


def parse_skill_md(text: str) -> tuple[str, str, str]:
    """(name, description, body) of a `SKILL.md`. Raises ExportError when the header is missing or incomplete."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ExportError("SKILL.md has no header")
    end = text.find("\n---", 4)
    if end == -1:
        raise ExportError("SKILL.md header is not closed")
    header, body = text[4:end], text[end + 4:]
    values: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            raise ExportError(f"SKILL.md header line is not key: value: {line!r}")
        raw = raw.strip()
        if raw.startswith('"'):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raise ExportError(f"SKILL.md header value is not valid: {line!r}") from None
        elif len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1]
        values[key.strip()] = raw
    if not values.get("name") or not values.get("description"):
        raise ExportError("SKILL.md header needs a name and a description")
    return values["name"], values["description"], _normalise(body)


def skills_dir(kind: str, root: Path | str) -> Path:
    """The folder where the engine `kind` looks for skills, under `root`."""
    try:
        layout = LAYOUTS[kind]
    except KeyError:
        raise ExportError(f"engine {kind!r} has no skills folder") from None
    return Path(root).joinpath(*layout)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".skill.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def export_skills(skills: list[Skill], kind: str, root: Path | str) -> list[Path]:
    """Write each skill under the skills folder of the engine `kind`. Returns the `SKILL.md` paths, in order.

    Two skills with the same name are an error. Existing files of the same name are overwritten, nothing else is touched."""
    names = [s.name for s in skills]
    if len(set(names)) != len(names):
        raise ExportError("two skills have the same name")
    base = skills_dir(kind, root)
    written: list[Path] = []
    for skill in skills:
        folder = base / skill.name
        target = folder / SKILL_FILE
        _write_atomic(target, render_skill_md(skill))
        for rel, text in skill.files.items():
            _write_atomic(folder.joinpath(*rel.split("/")), text + "\n")
        written.append(target)
    return written


def import_skills(kind: str, root: Path | str) -> list[Skill]:
    """Read back every skill under the skills folder of the engine `kind`, by name. A missing folder gives an empty list."""
    base = skills_dir(kind, root)
    if not base.is_dir():
        return []
    skills: list[Skill] = []
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        main = folder / SKILL_FILE
        if not main.is_file():
            continue
        name, description, body = parse_skill_md(main.read_text(encoding="utf-8"))
        if name != folder.name:
            raise ExportError(f"the folder {folder.name} holds the skill {name}")
        files: dict[str, str] = {}
        for extra in sorted(folder.rglob("*")):
            if not extra.is_file() or extra == main or extra.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}:
                continue
            files[extra.relative_to(folder).as_posix()] = extra.read_text(encoding="utf-8")
        skills.append(Skill(name, description, body, files))
    return skills


def export_for_active_engine(skills: list[Skill], root: Path | str, *, kind: str | None = None) -> tuple[str, list[Path]]:
    """Export to the engine the student runs now (or `kind`). The direct API has no skills folder: it raises ExportError."""
    if kind is None:
        from hokma.db import get_database
        from hokma.engine.dbstore import DbChatStore
        from hokma.engines import effective

        store = DbChatStore()
        with get_database().read() as u:
            default = u.engine_connections.default()
        chosen = effective.resolve(store._conn(default) if default else None, store.connections()).conn
        kind = "claude_code" if chosen is None else str(chosen.get("engine") or "claude_code")
    return kind, export_skills(skills, kind, root)

"""Skill format for every desk (SPEC-017: ED-036, OA-16, OA-17).

A skill is a Markdown file with a YAML frontmatter: name, version, description (with the phrases that trigger it),
identity, patterns and avoid. The package of skills has a manifest that records the schema, the package version and the
version and digest of each skill. The format is documented in `kit/template/desk/skill-format.md`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
MANIFEST_NAME = "skills-manifest.json"
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
REQUIRED = ("name", "version", "description", "identity", "patterns", "avoid")


class SkillError(ValueError):
    """A skill file or manifest that cannot be used. The message names the file and the problem."""


def split_frontmatter(text: str, source: str = "<text>") -> tuple[dict[str, Any], str]:
    """Split a Markdown text into its YAML frontmatter (a mapping) and its body."""
    found = _FRONT.match(text or "")
    if not found:
        raise SkillError(f"{source}: the file must start with a YAML frontmatter between two '---' lines")
    try:
        meta = yaml.safe_load(found.group(1))
    except yaml.YAMLError as exc:
        raise SkillError(f"{source}: the frontmatter is not valid YAML ({exc})") from exc
    if not isinstance(meta, dict):
        raise SkillError(f"{source}: the frontmatter must be a mapping")
    return meta, text[found.end():]


def _text_list(meta: dict[str, Any], key: str, source: str) -> tuple[str, ...]:
    value = meta.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(i, str) and i.strip() for i in value):
        raise SkillError(f"{source}: '{key}' must be a non-empty list of texts")
    return tuple(i.strip() for i in value)


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    description: str
    identity: str
    patterns: tuple[str, ...]
    avoid: tuple[str, ...]
    body: str
    source: str = ""

    def digest(self) -> str:
        payload = json.dumps([self.name, self.version, self.description, self.identity, self.patterns, self.avoid, self.body.strip()],
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_skill(text: str, source: str = "<text>") -> Skill:
    """A valid skill is returned; a missing or malformed field raises SkillError naming the field."""
    meta, body = split_frontmatter(text, source)
    for key in REQUIRED:
        if key not in meta or meta[key] in (None, "", []):
            raise SkillError(f"{source}: missing required field '{key}'")
    unknown = set(meta) - set(REQUIRED)
    if unknown:
        raise SkillError(f"{source}: unknown field(s) {sorted(unknown)}")
    name, version = meta["name"], str(meta["version"])
    if not isinstance(name, str) or not _NAME.match(name):
        raise SkillError(f"{source}: 'name' must be lowercase words joined by hyphens")
    if not _VERSION.match(version):
        raise SkillError(f"{source}: 'version' must look like 1.0.0")
    description = str(meta["description"]).strip()
    if len(description) < 20 or not re.search(r"\buse\b", description, re.I):
        raise SkillError(f"{source}: 'description' must say when to use the skill, with the phrases that trigger it")
    identity = str(meta["identity"]).strip()
    return Skill(name=name, version=version, description=" ".join(description.split()), identity=identity,
                 patterns=_text_list(meta, "patterns", source), avoid=_text_list(meta, "avoid", source),
                 body=body.strip(), source=source)


def load_skill(path: Path | str) -> Skill:
    path = Path(path)
    skill = parse_skill(path.read_text(encoding="utf-8"), str(path))
    if path.name.upper() == "SKILL.MD" and path.parent.name != skill.name:
        raise SkillError(f"{path}: the folder name must equal the skill name '{skill.name}'")
    return skill


def load_skills(folder: Path | str) -> list[Skill]:
    """Every `<folder>/<name>/SKILL.md`, sorted by name. A duplicate name is an error."""
    skills = [load_skill(p) for p in sorted(Path(folder).glob("*/SKILL.md"))]
    names = [s.name for s in skills]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SkillError(f"duplicate skill name(s): {dupes}")
    return sorted(skills, key=lambda s: s.name)


def build_manifest(skills: list[Skill], package_version: str) -> dict[str, Any]:
    if not _VERSION.match(package_version):
        raise SkillError("package version must look like 1.0.0")
    return {"schema_version": SCHEMA_VERSION, "package_version": package_version,
            "skills": {s.name: {"version": s.version, "digest": s.digest()} for s in sorted(skills, key=lambda s: s.name)}}


def write_manifest(folder: Path | str, package_version: str) -> dict[str, Any]:
    folder = Path(folder)
    manifest = build_manifest(load_skills(folder), package_version)
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def read_manifest(folder: Path | str) -> dict[str, Any]:
    path = Path(folder) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillError(f"{path}: manifest unreadable ({exc})") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("skills"), dict):
        raise SkillError(f"{path}: unsupported manifest schema")
    return data


def verify_manifest(folder: Path | str) -> list[str]:
    """Differences between the manifest and the skills on disk: added, removed, changed or version drift."""
    manifest = read_manifest(folder)["skills"]
    current = {s.name: s for s in load_skills(folder)}
    problems = [f"{n}: on disk but not in the manifest" for n in sorted(set(current) - set(manifest))]
    problems += [f"{n}: in the manifest but not on disk" for n in sorted(set(manifest) - set(current))]
    for name in sorted(set(current) & set(manifest)):
        entry, skill = manifest[name], current[name]
        if entry.get("digest") != skill.digest():
            same = entry.get("version") == skill.version
            problems.append(f"{name}: content changed" + (" without a version bump" if same else ""))
    return problems

"""Agent files in the working folder.

Layout (all under the working folder, `hokma_home()`):

    agents/<slug>.md            the source of each agent: what the kit installs and what the database seeds read.
                                The model tier is rewritten here.
    .claude/agents/<slug>.md    the copy Claude Code loads (`--agent`). Present only while the agent is on.
    .claude/agents.off/<slug>.md  where the copy goes when the agent is turned off.

Every read and write goes through `hokma.kit.safefs`, so a symlink planted by an agent is never followed.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from hokma.core.config import hokma_home
from hokma.kit import safefs

SOURCE_DIR = "agents"
LOADED_DIR = ".claude/agents"
OFF_DIR = ".claude/agents.off"
SKILLS_DIR = ".claude/skills"

TIER_MODEL = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}
MODEL_TIER = {model: tier for tier, model in TIER_MODEL.items()}
MODELS = tuple(MODEL_TIER)

MAX_NAME = 60
MAX_DESCRIPTION = 500
MAX_INSTRUCTIONS = 8000
CUSTOM_TOOLS = ("Read, Grep, Glob, Write, Edit, mcp__hokma__journal_write, mcp__hokma__journal_search, "
                "mcp__hokma__task_list, mcp__hokma__task_show, mcp__hokma__task_create")

MAX_SKILL = 20000
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-*().:]{0,100}$")

# Characters that would turn a plain YAML value into something else (or inject a marker).
_FORBIDDEN_NAME = re.compile(r"[:#{}\[\]|<>&*!%@`'\"\\\x00-\x1f\x7f]")
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")
_FRONT_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")


class AgentFileError(Exception):
    """A user-facing problem with an agent file. `code` is translated by the API layer."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def clean_name(value: object) -> str:
    """One line, no YAML or marker characters, bounded length."""
    if not isinstance(value, str):
        return ""
    return " ".join(_FORBIDDEN_NAME.sub(" ", value).split())[:MAX_NAME].strip()


def clean_text(value: object, limit: int) -> str:
    """Free text: keeps lines, drops control characters (bounded)."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32 and ord(ch) != 127)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_JUNK.sub("-", ascii_name).strip("-")[:40].strip("-")


def split_frontmatter(text: str) -> tuple[list[str], str] | None:
    """(frontmatter lines without the fences, body) or None when the file has no frontmatter."""
    lines = text.lstrip("﻿").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], "\n".join(lines[index + 1:])
    return None


def set_frontmatter_value(text: str, key: str, value: str) -> str:
    """Replace the top-level `key: value` line (adding it before the closing fence); the rest is untouched."""
    parts = split_frontmatter(text)
    if parts is None:
        raise AgentFileError("agent_file_invalid", "no frontmatter")
    head, body = parts
    replaced = False
    for index, line in enumerate(head):
        match = _FRONT_LINE.match(line)
        if match and match.group(1) == key and not line.startswith((" ", "\t")):
            head[index] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        head.append(f"{key}: {value}")
    return "---\n" + "\n".join(head) + "\n---\n" + body


def frontmatter_value(text: str, key: str) -> str | None:
    parts = split_frontmatter(text)
    if parts is None:
        return None
    for line in parts[0]:
        match = _FRONT_LINE.match(line)
        if match and match.group(1) == key and not line.startswith((" ", "\t")):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def clean_tools(value: object) -> str:
    """The `tools:` line of an agent: a comma list (or a list) of plain tool names; empty or odd input gives the default."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    names: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and _TOOL_NAME.match(name) and name not in names:
            names.append(name)
    return ", ".join(names[:40]) or CUSTOM_TOOLS


def build_custom_agent(slug: str, name: str, description: str, model: str, instructions: str,
                       tools: str | None = None) -> str:
    """Text of the .md of an agent the owner creates. The description is a folded block, so any text is valid YAML."""
    wrapped = "\n".join("  " + line for line in " ".join(description.split()).splitlines() or [""])
    return (f"---\nname: {slug}\ndisplay_name: {name}\nrole: custom\nslug: {slug}\ndescription: >\n{wrapped}\n"
            f"model: {model}\nhokma-tier: {MODEL_TIER[model]}\ntools: {clean_tools(tools) if tools else CUSTOM_TOOLS}\n"
            f"---\n\n{instructions.strip()}\n")


def read_custom_agent(text: str) -> dict[str, str] | None:
    """Fields of an agent file built by `build_custom_agent` (None when it has no frontmatter)."""
    parts = split_frontmatter(text)
    if parts is None:
        return None
    head, body = parts
    description, folded = [], False
    for line in head:
        if line.startswith((" ", "\t")):
            if folded:
                description.append(line.strip())
            continue
        folded = line.startswith("description:") and line.split(":", 1)[1].strip() in (">", "|", ">-", "|-")
        if line.startswith("description:") and not folded:
            description = [line.split(":", 1)[1].strip().strip("'\"")]
    return {"display_name": frontmatter_value(text, "display_name") or "", "description": " ".join(description),
            "model": frontmatter_value(text, "model") or "sonnet", "tools": frontmatter_value(text, "tools") or "",
            "instructions": body.strip()}


def build_skill(slug: str, description: str, instructions: str) -> str:
    """SKILL.md: `name` is the folder name, the description is a folded block."""
    wrapped = "\n".join("  " + line for line in " ".join(description.split()).splitlines() or [""])
    return f"---\nname: {slug}\ndescription: >\n{wrapped}\n---\n\n{instructions.strip()}\n"


class AgentFiles:
    def __init__(self, workdir: Path | None = None) -> None:
        self.root = workdir if workdir is not None else hokma_home()

    # ------------------------------------------------------------------ low level

    def _read(self, rel: str) -> str | None:
        try:
            data = safefs.read_regular(self.root, rel)
        except safefs.UnsafePath as exc:
            raise AgentFileError("agent_file_unsafe", str(exc)) from exc
        return None if data is None else data.decode("utf-8")

    def _write(self, rel: str, text: str) -> None:
        try:
            safefs.write_atomic(self.root, rel, text.encode("utf-8"), 0o644)
        except safefs.UnsafePath as exc:
            raise AgentFileError("agent_file_unsafe", str(exc)) from exc

    def _remove(self, rel: str) -> None:
        if self._read(rel) is None:                        # also proves the chain has no symlink
            return
        try:
            os.unlink(self.root / rel)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ public

    def source_rel(self, slug: str, file_path: str | None) -> str:
        rel = file_path or f"{SOURCE_DIR}/{slug}.md"
        parts = Path(rel).parts
        if len(parts) != 2 or parts[0] != SOURCE_DIR or not rel.endswith(".md") or ".." in parts:
            raise AgentFileError("agent_file_unsafe", f"unexpected agent file path {rel!r}")
        return rel

    def read_source(self, slug: str, file_path: str | None) -> str | None:
        return self._read(self.source_rel(slug, file_path))

    def write_source(self, slug: str, file_path: str | None, text: str) -> None:
        self._write(self.source_rel(slug, file_path), text)

    def is_loaded(self, slug: str) -> bool:
        return self._read(f"{LOADED_DIR}/{slug}.md") is not None

    def apply(self, slug: str, file_path: str | None, *, active: bool, model: str | None = None) -> bool:
        """Make the disk match: the model in the source and the loaded copy present (on) or moved aside (off).
        Returns False when the agent has no source file (nothing to load)."""
        text = self.read_source(slug, file_path)
        if text is None:
            text = self._read(f"{LOADED_DIR}/{slug}.md") or self._read(f"{OFF_DIR}/{slug}.md")
            if text is None:
                return False
        if model is not None:
            tier = MODEL_TIER[model]
            if frontmatter_value(text, "model") != model or frontmatter_value(text, "hokma-tier") != tier:
                text = set_frontmatter_value(set_frontmatter_value(text, "model", model), "hokma-tier", tier)
        if self._read(self.source_rel(slug, file_path)) != text:
            self.write_source(slug, file_path, text)
        if active:
            if self._read(f"{LOADED_DIR}/{slug}.md") != text:
                self._write(f"{LOADED_DIR}/{slug}.md", text)
            self._remove(f"{OFF_DIR}/{slug}.md")
        else:
            if self._read(f"{OFF_DIR}/{slug}.md") != text:
                self._write(f"{OFF_DIR}/{slug}.md", text)
            self._remove(f"{LOADED_DIR}/{slug}.md")
        return True

    def skill_rel(self, slug: str) -> str:
        if not slug or slug != slugify(slug):
            raise AgentFileError("agent_file_unsafe", f"unexpected skill name {slug!r}")
        return f"{SKILLS_DIR}/{slug}/SKILL.md"

    def read_skill(self, slug: str) -> str | None:
        return self._read(self.skill_rel(slug))

    def write_skill(self, slug: str, text: str) -> None:
        self._write(self.skill_rel(slug), text)

    def model_of(self, slug: str, file_path: str | None) -> str | None:
        text = self.read_source(slug, file_path)
        return frontmatter_value(text, "model") if text else None

    def role_of(self, slug: str, file_path: str | None) -> str | None:
        text = self.read_source(slug, file_path)
        return frontmatter_value(text, "role") if text else None

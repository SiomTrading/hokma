"""The student's skills and agents, put where `--setting-sources user` does read them.

Claude Code is launched with `--setting-sources user`, and that flag also decides where it looks for skills and
agents: the working folder's `.claude/skills` and `.claude/agents` (project source) are never entered, so a skill with
a SKILL.md and `--agent <name>` or a dispatch to another agent answered "not found". 'project' is not added to the
sources (it would load settings the agent can edit). Instead Hokma copies what is in the working folder into the user
source, which is inside its protected configuration folder (`<config>/skills`, `<config>/agents`), on every launch:

- agents: `.claude/agents/<slug>.md` (the active ones; `agents.off` is not copied);
- skills: `.claude/skills/<name>/` (SKILL.md and its plain files).

What comes across is the content, not the power: only the frontmatter keys listed in ALLOWED_* survive (and `name` is always the file's own name), so a file
written by the agent cannot bring hooks, a permission mode or MCP servers with it. Nothing is read through a link, a
file that is not regular or one with several links. A manifest (`hokma-mirror.json`) lists what Hokma wrote, so what
the student deleted disappears from the user source, and a skill or agent the student put there by hand is never
overwritten or removed.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from hokma.core.config import claude_config_dir, hokma_home
from hokma.kit import safefs

MANIFEST = "hokma-mirror.json"
MAX_FILE = 100_000
MAX_SKILL_FILES = 40
MAX_SKILL_BYTES = 1_000_000
ALLOWED_AGENT = ("description", "tools", "model", "color")
ALLOWED_SKILL = ("description", "when_to_use", "argument-hint", "disable-model-invocation", "user-invocable")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LINE_BREAKS = re.compile("[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029]")   # YAML breaks lines on more than \\n
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")


def _filter_frontmatter(text: str, allowed: tuple[str, ...], default_name: str) -> str | None:
    """The text with only the allowed frontmatter keys (and their continuation lines); None without a frontmatter."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    if any(_LINE_BREAKS.search(line) for line in lines[: end + 1]):
        return None                # a lone CR, NEL or U+2028/9 would hide a key inside what looks like one line
    kept: list[str] = [f"name: {default_name}"]      # always the file's own name: a file cannot shadow another agent
    keep = False
    seen: set[str] = set()
    for line in lines[1:end]:
        if not line.strip():
            if keep:
                kept.append(line)
            continue
        m = _KEY.match(line)
        if not m and not (line[0] in " \t" or line.startswith("- ") or line.startswith("#")):
            return None            # `"hooks": x`, `hooks : x`, `? hooks`...: a key the line filter cannot recognise, so no file
        if m:
            key = m.group(1)
            keep = key in allowed and key not in seen          # `name` is not allowed: it is set above
            if keep:
                seen.add(key)
        if keep:
            kept.append(line)
    return "\n".join(["---", *kept, "---", *lines[end + 1:]])


def _scan(root: Path, rel: str) -> list[os.DirEntry]:
    """Entries of a folder under root, [] when it is missing or any part of the path is a link."""
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        try:
            if not stat.S_ISDIR(os.lstat(cur).st_mode):
                return []
        except OSError:
            return []
    try:
        with os.scandir(cur) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _plain(entry: os.DirEntry) -> bool:
    """A regular file with one link: not a symbolic link, not a hard link to a key stored somewhere else."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _read(root: Path, rel: str, limit: int = MAX_FILE) -> bytes | None:
    try:
        return safefs.read_regular(root, rel, max_bytes=limit, single_link=True)
    except (safefs.UnsafePath, OSError):
        return None


def _agents(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for entry in _scan(root, ".claude/agents"):
        slug = entry.name[:-3]
        if not entry.name.endswith(".md") or not _NAME.match(slug) or not _plain(entry):
            continue
        raw = _read(root, f".claude/agents/{entry.name}")
        text = _filter_frontmatter(raw.decode("utf-8", errors="replace"), ALLOWED_AGENT, slug) if raw is not None else None
        if text:
            out[slug] = text.encode("utf-8")
    return out


def _skills(root: Path) -> dict[str, dict[str, bytes]]:
    out: dict[str, dict[str, bytes]] = {}
    for entry in _scan(root, ".claude/skills"):
        if not _NAME.match(entry.name) or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        base = f".claude/skills/{entry.name}"
        main = _read(root, f"{base}/SKILL.md") if any(e.name == "SKILL.md" and _plain(e) for e in _scan(root, base)) else None
        text = _filter_frontmatter(main.decode("utf-8", errors="replace"), ALLOWED_SKILL, entry.name) if main is not None else None
        if not text:
            continue
        files, total = {"SKILL.md": text.encode("utf-8")}, 0
        stack = [""]
        while stack and len(files) < MAX_SKILL_FILES:
            sub = stack.pop()
            for item in _scan(root, f"{base}/{sub}".rstrip("/")):
                rel = f"{sub}/{item.name}".lstrip("/")
                if item.is_symlink() or item.name.startswith("."):
                    continue
                if item.is_dir(follow_symlinks=False):
                    stack.append(rel)
                elif _plain(item) and rel != "SKILL.md" and len(files) < MAX_SKILL_FILES:
                    data = _read(root, f"{base}/{rel}")
                    if data is not None and total + len(data) <= MAX_SKILL_BYTES:
                        files[rel], total = data, total + len(data)
        out[entry.name] = files
    return out


def _load_manifest(config: Path) -> dict:
    try:
        data = json.loads((config / MANIFEST).read_text(encoding="utf-8"))
        return {"agents": list(data.get("agents", [])), "skills": list(data.get("skills", []))}
    except (OSError, ValueError, AttributeError):
        return {"agents": [], "skills": []}


def _put(config: Path, rel: str, data: bytes) -> None:
    safefs.write_atomic(config, rel, data, 0o644)


def _remove(config: Path, rel: str) -> None:
    target = config / rel
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            for sub in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                sub.unlink() if sub.is_symlink() or sub.is_file() else sub.rmdir()
            target.rmdir()
    except OSError:
        pass


def sync(workdir: Path | None = None, config: Path | None = None) -> dict:
    """Copy the working folder's active agents and skills into the user source and drop what the student removed."""
    root, config = workdir or hokma_home(), config or claude_config_dir()
    config.mkdir(parents=True, exist_ok=True)
    old, new = _load_manifest(config), {"agents": [], "skills": []}
    agents, skills = _agents(root), _skills(root)
    for slug, data in agents.items():
        rel = f"agents/{slug}.md"
        if (config / rel).exists() and slug not in old["agents"]:
            continue                                       # the student's own user-level agent wins
        _put(config, rel, data)
        new["agents"].append(slug)
    for name, files in skills.items():
        if (config / "skills" / name).exists() and name not in old["skills"]:
            continue
        if name in old["skills"]:
            _remove(config, f"skills/{name}")              # ours: files the student deleted must not linger
        for sub, data in files.items():
            _put(config, f"skills/{name}/{sub}", data)
        new["skills"].append(name)
    for slug in old["agents"]:
        if slug not in new["agents"]:
            _remove(config, f"agents/{slug}.md")
    for name in old["skills"]:
        if name not in new["skills"]:
            _remove(config, f"skills/{name}")
    (config / MANIFEST).write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return new

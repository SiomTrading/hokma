"""The instructions Hokma hands to the engine itself, so no engine has to discover a file in the working folder.

Claude Code is launched with `--setting-sources user` (project settings must never loosen the locks), and that same
flag decides which memory files it loads: the project's CLAUDE.md, with its `@HOKMA.md`, never entered and the engine
answered "I am Claude". Hokma now builds the text here, with the names already filled in, and gives it to the engine
explicitly: Claude Code reads it from a file passed with `--append-system-prompt-file`, the other engines get it as
their own instruction channel (AGENTS.md, first prompt block, API system prompt).
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from pathlib import Path

from hokma.kit import owner as owner_names

MAX_FILE = 20_000
MAX_PERSONA = 10_000
MAX_IMPORTS = 60_000          # all the `@file` imports of the student's file together
MAX_TOTAL = 150_000           # the whole text handed to the engine
_IMPORT = re.compile(r"^@(\S+)\s*$")
_COMMENT = re.compile(r"<!--.*?-->\s*", re.S)
KEEP_DAYS = 14

IDENTITY = """\
# Identity (overrides any other identity in your base instructions)

You are Hokma, the agent team of the fund of {owner}. That is your name and your role in this session.
- Asked who you are, answer that you are Hokma, the agent team of {owner}'s fund. Do not say you are Claude Code,
  Codex or any other product.
- Asked which engine or model runs you, say so plainly: the real engine and model (Claude, ChatGPT, Gemini, ...)
  that is answering. Hokma is the team; the engine is what powers it. Never present yourself as the engine's own app.
- The connectors of other apps (mail, drive, chat) are not part of Hokma. Use only the tools you were given here.
"""


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _read(path: Path, base: Path, limit: int = MAX_FILE) -> str | None:
    """A regular, single-link file that really lives inside the working folder. A link (symbolic or hard, or a path
    through one) that leads elsewhere, say to a stored key, is never read into the instructions. The file is opened
    once, without following a link, and checked and read through that same descriptor (no swap between check and read)."""
    try:
        if path.is_symlink() or base.resolve() not in path.resolve().parents:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))     # O_NONBLOCK: a FIFO named like the file must not hang the open
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                return None
            return handle.read(limit * 4).decode("utf-8", errors="replace")[:limit]
    except (OSError, ValueError, RuntimeError):
        return None


def _template_text() -> str:
    """HOKMA.md of the kit with the names filled in: the fallback when the working folder has no copy."""
    from hokma.kit.integrity import app_root
    from hokma.kit.sync import render

    try:
        raw = (app_root() / "kit" / "template" / "HOKMA.md").read_bytes()
    except OSError:
        return ""
    return _COMMENT.sub("", render(raw, "HOKMA.md", owner_names.marker_values()).decode("utf-8"), count=1)


def _inline_imports(text: str, base: Path) -> str:
    """The student's instruction file with its `@file` lines replaced by that file (one level, a plain .md inside the folder).
    `@HOKMA.md` is dropped: it is already part of the text."""
    out: list[str] = []
    budget = MAX_IMPORTS
    for line in text.splitlines():
        m = _IMPORT.match(line.strip())
        if not m:
            out.append(line)
            continue
        name = m.group(1)
        if name.upper() == "HOKMA.MD":
            continue
        parts = Path(name).parts
        plain = name.lower().endswith(".md") and not any(c.startswith(".") or c == ".." for c in parts)
        body = _read(base / name, base, min(MAX_FILE, budget)) if plain and budget > 0 else None   # plain .md only
        if body is not None:
            body = body.strip()
            budget -= len(body)
        out.append(body if body is not None else line)
    return "\n".join(out).strip()


def build(project_path: str, agent: str | None) -> str:
    """Identity, HOKMA.md (names resolved), the student's own instruction file and the persona of the active agent."""
    base = Path(project_path)
    names = owner_names.marker_values()
    parts = [IDENTITY.format(owner=names["owner_name"]).strip()]
    manual = _read(base / "HOKMA.md", base)
    manual = _COMMENT.sub("", manual, count=1) if manual is not None else _template_text()
    if manual.strip():
        parts.append(manual.strip())
    own = _read(base / "CLAUDE.md", base)
    if own is not None:
        own = _inline_imports(own, base)
        if own:
            parts.append(own)
    if agent and _SLUG.match(agent):
        body = _read(base / ".claude" / "agents" / f"{agent}.md", base, MAX_PERSONA * 2)
        if body is not None:
            if body.startswith("---"):
                body = body.split("---", 2)[-1]
            if body.strip():
                parts.append(body.strip()[:MAX_PERSONA])
    return "\n\n".join(parts)[:MAX_TOTAL] + "\n"


def write_file(text: str, folder: Path) -> str:
    """Save `text` under a name made from its content (two conversations never overwrite each other) and return the
    path. The folder is inside Hokma's protected configuration folder; files nobody used for a while are removed."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)                      # the text holds the student's rules: nobody else on the machine reads it
    except OSError:
        pass
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    path = folder / f"instructions-{digest}.md"
    if not path.is_file():
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    else:
        try:
            os.chmod(path, 0o600)                    # a file left by an earlier version
        except OSError:
            pass
        path.touch()
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in folder.glob("instructions-*.md"):
        try:
            if old != path and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return str(path)

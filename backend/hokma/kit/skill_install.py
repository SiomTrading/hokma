"""Install a skill from a GitHub link, with any engine (task 3175).

A student wrote "install this skill https://github.com/NVIDIA/SkillSpector.git" and an engine without a terminal
answered that it could not. This is the one way, used by the `skill_install` tool of Hokma's MCP server (every
engine has it) and by the "Add skill by link" button in Settings > Extensions:

1. the link is read: a repository (`https://github.com/owner/repo`, with or without `.git`) or a folder inside it
   (`/tree/<branch>/<path>`, `/blob/<branch>/<path>/SKILL.md`);
2. the repository is downloaded as a zip from GitHub (no git, so nothing can ask for a password), at most
   `MAX_DOWNLOAD` bytes, and unpacked into a temporary folder inside the fund (`<fund>/.hokma/tmp`), refusing links,
   absolute paths and `..`;
3. what is there decides: a folder with SKILL.md is one skill; a repository that is a plugin or a marketplace
   (`.claude-plugin/`) goes to the plugin installation (the same as Settings > Extensions > Add by link); otherwise
   every folder with a SKILL.md below it is a skill;
4. each skill is copied into `<fund>/.claude/skills/<name>/` with the checks of the mirror (`kit/mirror.py`): only
   the allowed frontmatter keys (no hooks, permission mode or MCP servers), regular files only, no hidden files,
   the same size limits; then the mirror runs, so Claude Code sees it at once and the other engines read it from the
   working folder.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from hokma.i18n import t
from hokma.kit import mirror, safefs

MAX_DOWNLOAD = 60 * 1024 * 1024
MAX_UNPACKED = 300 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_SKILLS = 50
TIMEOUT = 40
HOSTS = {"github.com", "www.github.com", "codeload.github.com"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class SkillInstallError(ValueError):
    """The install failed; the message says why and what to do, in plain words."""


# ---------------------------------------------------------------- the link

def _safe_path(path: str) -> bool:
    """Every part of a folder path inside the repository: no empty, `.`, `..`, `\\`, `:` (a Windows drive or stream),
    no absolute path (POSIX or Windows) and no control characters."""
    if not path or path.startswith(("/", "\\")):
        return False
    return all(part not in ("", ".", "..") and not any(c in part for c in "\\:") and part.isprintable()
               for part in path.split("/"))


def parse_link(link: str) -> dict[str, str]:
    """{'owner', 'repo', 'ref' ('' = default branch), 'path' ('' = the whole repository)}."""
    raw = (link or "").strip().strip("<>").strip()
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", raw):
        raw = "https://github.com/" + raw
    if raw.lower().startswith(("github.com/", "www.github.com/")):
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in ("https", "http") or (parsed.hostname or "").lower() not in ("github.com",
                                                                                                    "www.github.com"):
        raise SkillInstallError("only GitHub links are accepted, like https://github.com/owner/repo or a folder "
                                "inside it (https://github.com/owner/repo/tree/main/skills/name)")
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise SkillInstallError("the link needs the owner and the repository: https://github.com/owner/repo")
    owner, repo = parts[0], parts[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not _PART.match(owner) or not _PART.match(repo):
        raise SkillInstallError("the owner or the repository name in the link is not valid")
    ref, path = "", ""
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        ref = parts[3]
        rest = parts[4:]
        if rest and rest[-1].lower() in ("skill.md", "readme.md") or (parts[2] == "blob" and rest):
            rest = rest[:-1]
        path = "/".join(rest)
    elif len(parts) > 2:
        raise SkillInstallError("the link is not a repository or a folder of one; copy it from the address bar of "
                                "the repository or folder on GitHub")
    if ref and (ref.startswith("-") or ".." in ref or not re.match(r"^[A-Za-z0-9_./-]{1,100}$", ref)):
        raise SkillInstallError("the branch in the link is not valid")
    if path and not _safe_path(path):
        raise SkillInstallError("the folder in the link is not valid")
    return {"owner": owner, "repo": repo, "ref": ref, "path": path}


# ---------------------------------------------------------------- download and unpack

class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):        # noqa: D401
        host = (urllib.parse.urlparse(newurl).hostname or "").lower()
        if urllib.parse.urlparse(newurl).scheme != "https" or host not in HOSTS:
            raise SkillInstallError(f"GitHub sent the download somewhere else ({host}); it was not followed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(owner: str, repo: str, ref: str = "") -> bytes:
    """The repository as a zip. No sign-in is ever asked: a private or missing repository fails at once."""
    name = urllib.parse.quote(ref or "HEAD", safe="")
    url = f"https://github.com/{owner}/{repo}/archive/{name}.zip"
    opener = urllib.request.build_opener(_GitHubOnly)
    request = urllib.request.Request(url, headers={"User-Agent": "Hokma"})
    try:
        with opener.open(request, timeout=TIMEOUT) as resp:                     # noqa: S310
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD:
                raise SkillInstallError(f"the repository is larger than {MAX_DOWNLOAD // (1024 * 1024)} MB")
            data = resp.read(MAX_DOWNLOAD + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise SkillInstallError(f"GitHub did not give {owner}/{repo}" + (f" (branch {ref})" if ref else "") +
                                    ": it does not exist or it is private. Hokma downloads public repositories only "
                                    "and never asks for a password") from None
        raise SkillInstallError(f"GitHub answered {exc.code} for {owner}/{repo}; try again later") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SkillInstallError(f"could not reach GitHub ({reason}); check the internet connection") from None
    if len(data) > MAX_DOWNLOAD:
        raise SkillInstallError(f"the repository is larger than {MAX_DOWNLOAD // (1024 * 1024)} MB")
    return data


def unpack(data: bytes, dest: Path) -> Path:
    """Unpack the GitHub zip into `dest`; returns the repository folder (the zip's single top folder)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillInstallError("GitHub did not send a valid zip") from None
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise SkillInstallError("the repository has too many files")
    total = 0
    tops: set[str] = set()
    for info in members:
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in pure.parts:
            continue                                              # never outside the folder
        if stat.S_ISLNK(info.external_attr >> 16):
            continue                                              # links are not copied, like the mirror
        if not pure.parts or any(":" in part for part in pure.parts):
            continue                                              # a drive-qualified part would leave the folder on Windows
        tops.add(pure.parts[0])
        target = dest.joinpath(*pure.parts)
        if not target.resolve().is_relative_to(dest.resolve()):
            continue
        if info.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            continue
        total += info.file_size
        if total > MAX_UNPACKED:
            raise SkillInstallError(f"the repository is larger than {MAX_UNPACKED // (1024 * 1024)} MB unpacked")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        except OSError:
            continue                                              # a name this system cannot hold (Windows: `a:b`)
    if len(tops) != 1:
        raise SkillInstallError("the zip from GitHub does not have the expected shape")
    return dest / tops.pop()


# ---------------------------------------------------------------- what is there

def _is_plain_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_plain_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def find_skills(root: Path) -> list[Path]:
    """Folders under `root` with a SKILL.md, top first; a skill's own subfolders are not searched."""
    found: list[Path] = []
    stack = [root]
    while stack and len(found) <= MAX_SKILLS:
        folder = stack.pop(0)
        if _is_plain_file(folder / "SKILL.md"):
            found.append(folder)
            continue
        try:
            subs = sorted(p for p in folder.iterdir() if p.name not in SKIP_DIRS and _is_plain_dir(p))
        except OSError:
            continue
        stack.extend(subs)
    return found


def kind_of(folder: Path) -> str:
    """'skill' (SKILL.md right there), 'marketplace' or 'plugin' (`.claude-plugin/`), else 'folder'."""
    if _is_plain_file(folder / "SKILL.md"):
        return "skill"
    if _is_plain_file(folder / ".claude-plugin" / "marketplace.json"):
        return "marketplace"
    if _is_plain_file(folder / ".claude-plugin" / "plugin.json"):
        return "plugin"
    return "folder"


def skill_name(folder: Path, fallback: str) -> str:
    """The `name:` of the frontmatter when it is a valid folder name, else the folder's own name."""
    try:
        head = (folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        head = ""
    match = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", head.split("\n---", 1)[0], re.M)
    for candidate in ((match.group(1) if match else ""), folder.name, fallback):
        slug = _SLUG.sub("-", candidate.strip()).strip("-.")[:64]
        if mirror._NAME.match(slug):
            return slug
    raise SkillInstallError("the skill has no usable name")


def collect(folder: Path, name: str) -> tuple[dict[str, bytes], int]:
    """The files of one skill as the mirror accepts them; (files, files left out)."""
    raw = (folder / "SKILL.md").read_bytes()
    if len(raw) > mirror.MAX_FILE:
        raise SkillInstallError(f"the SKILL.md of {name} is larger than {mirror.MAX_FILE} bytes")
    text = mirror._filter_frontmatter(raw.decode("utf-8", errors="replace"), mirror.ALLOWED_SKILL, name)
    if not text:
        raise SkillInstallError(f"the SKILL.md of {name} has no valid frontmatter (a block between --- lines with "
                                "at least `description:`), so no engine would load it")
    files: dict[str, bytes] = {"SKILL.md": text.encode("utf-8")}
    left, total = 0, 0
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        parts = rel.split("/")
        if rel == "SKILL.md" or any(p.startswith(".") or p in SKIP_DIRS for p in parts):
            continue
        if os.path.islink(path) or not _is_plain_file(path):
            continue
        size = path.stat().st_size
        if len(files) >= mirror.MAX_SKILL_FILES or size > mirror.MAX_FILE or total + size > mirror.MAX_SKILL_BYTES:
            left += 1
            continue
        files[rel] = path.read_bytes()
        total += size
    return files, left


# ---------------------------------------------------------------- the install

def _skills_root(workdir: Path) -> Path:
    return workdir / ".claude" / "skills"


def _make_room(workdir: Path, name: str, replace: bool) -> bool:
    """True when a skill of that name was there and went to the Hokma trash (never deleted)."""
    target = _skills_root(workdir) / name
    if not os.path.lexists(target):
        return False
    if not replace:
        raise SkillInstallError(f"a skill named {name} already exists; to replace it, call again with replace "
                                "set to true (the old one goes to the Hokma trash)")
    from hokma.db import current_version, get_database
    from hokma.kit.settings import protected_paths
    from hokma.permissions import trash

    if current_version() == 0:
        raise SkillInstallError("the old skill cannot go to the trash before the first-use setup is finished")
    try:
        trash.move([str(target)], f"replaced by the skill installed from a link: {name}", db=get_database(),
                   roots=[str(workdir)], protect=[str(p) for p in protected_paths()], cwd=str(workdir))
    except trash.TrashError as exc:
        raise SkillInstallError(f"the old skill {name} could not go to the trash: {exc.message}") from None
    return True


def _plugin_route(link: dict[str, str], kind: str) -> dict[str, Any]:
    """A plugin or a marketplace: the same path as Settings > Extensions > Add by link."""
    from hokma.status import extensions_service as service

    repo = f"{link['owner']}/{link['repo']}"
    try:
        added = service.add_marketplace(repo)
    except service.ServiceError as exc:
        raise SkillInstallError(f"{repo} is a {kind}, not a skill, and adding it as a plugin failed: "
                                f"{exc.message}") from None
    from hokma.status import plugin_install

    installed = added.get("installed", [])
    for plugin_id in installed:
        plugin_install.log(f"installed from the chat: plugin {plugin_id} from {repo} (brings hooks/MCP)")
        plugin_install.record_chat_install(plugin_id, repo)
    notice = "".join(t("skill.notice.plugin_installed", plugin=pid.split("@")[0], repo=repo) + " "
                     for pid in installed).strip()
    return {"kind": kind, "repository": repo, "marketplace": added.get("name"),
            "installed_plugins": installed, "notice": notice,
            "plugins": [p.get("id") or p.get("name") for p in added.get("plugins", [])],
            "warnings": added.get("warnings", []),
            "note": (("SHOW THIS NOTICE TO THE OWNER in your answer: " + notice + " " if notice else "") +
                     "it is a plugin repository: it was added to Settings > Extensions" +
                     (" and installed" if added.get("installed") else "; install the plugins you want there"))}


def install(link: str, replace: bool = False, workdir: Path | None = None, config: Path | None = None
            ) -> dict[str, Any]:
    """Install the skill (or skills) a GitHub link points at. Returns what was installed, for the chat and the
    panel; raises SkillInstallError with the reason."""
    from hokma.core.config import hokma_home

    root = Path(workdir) if workdir is not None else hokma_home()
    parsed = parse_link(link)
    data = download(parsed["owner"], parsed["repo"], parsed["ref"])
    tmp_parent = root / ".hokma" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="skill-", dir=tmp_parent))
    try:
        repo_dir = unpack(data, tmp)
        if parsed["path"] and not _safe_path(parsed["path"]):
            raise SkillInstallError("the folder in the link is not valid")
        start = repo_dir.joinpath(*parsed["path"].split("/")) if parsed["path"] else repo_dir
        if not start.resolve().is_relative_to(repo_dir.resolve()):
            raise SkillInstallError("the folder in the link is not valid")
        if not _is_plain_dir(start):
            raise SkillInstallError(f"the folder {parsed['path']} is not in {parsed['owner']}/{parsed['repo']}")
        kind = kind_of(start)
        if kind in ("marketplace", "plugin") and not parsed["path"]:
            return _plugin_route(parsed, kind)
        folders = [start] if kind == "skill" else find_skills(start)
        if not folders:
            raise SkillInstallError(f"there is no SKILL.md in {parsed['owner']}/{parsed['repo']}"
                                    + (f"/{parsed['path']}" if parsed["path"] else "") +
                                    ": it is not a skill. If it is a program, follow its README instead")
        if len(folders) > MAX_SKILLS:
            raise SkillInstallError(f"the repository has more than {MAX_SKILLS} skills; give the link of the folder "
                                    "of the one you want")
        prepared: list[tuple[str, dict[str, bytes], int]] = []
        names: set[str] = set()
        for folder in folders:
            name = skill_name(folder, parsed["repo"] if folder == repo_dir else folder.name)
            if name in names:
                continue
            names.add(name)
            files, left = collect(folder, name)
            prepared.append((name, files, left))
        installed = []
        for name, files, left in prepared:
            replaced = _make_room(root, name, replace)
            for rel, body in files.items():
                safefs.write_atomic(root, f".claude/skills/{name}/{rel}", body, 0o644)
            installed.append({"name": name, "path": f".claude/skills/{name}", "files": len(files),
                              "left_out": left, "replaced": replaced})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)             # Hokma's own temporary download, not a student file
    mirrored: list[str] = []
    try:
        mirrored = mirror.sync(root, config)["skills"]
    except Exception:                                      # noqa: BLE001  the next launch mirrors it anyway
        pass
    return {"kind": "skill", "repository": f"{parsed['owner']}/{parsed['repo']}", "skills": installed,
            "available_now": [s["name"] for s in installed if s["name"] in mirrored],
            "note": "installed in the working folder; it can be used in this conversation and the next ones"}

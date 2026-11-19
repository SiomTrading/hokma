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
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from hokma.core import tls
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


PY_TIMEOUT = 600
# The ONLY Python-package skills Hokma installs: repo -> exact PyPI package, pinned version and the setup step (exact
# command, declared here, run on the installed package). Nothing a repository declares picks a package, a version or a
# command; a repo that is not here with a pyproject.toml is refused (installing it would run its build code). Pinned
# to the latest release verified on PyPI (2026-11-19: graphifyy 0.9.112, not yanked; sdist sha256
# 67aad04f70aa3afc9ff6961f82d7cca5b04fb6b9446a3cddcb7b16483fbdb59b, wheel sha256
# ad6a9152fb6d9c5d85a77ffcaf29e18c5634a7d5ca474edb218cbc68b062241d). `uv tool install` has no hash option, so the hashes
# are documentation only; bump the version here after checking a new release.
PYPI_ALLOW: dict[str, dict[str, Any]] = {
    "safishamsi/graphify": {"package": "graphifyy", "version": "0.9.112",
                            "setup": ["graphify", "install", "--platform", "claude"]},
}
# Commands a package may not leave in the uv-tools bin (checked on the real files after installing, whatever
# declared them): they would shadow tools Hokma, the engines or the owner use.
RESERVED_COMMANDS = {"ls", "cat", "rm", "cp", "mv", "gh", "git", "make", "docker", "cmd", "powershell", "pwsh", "where",
                     "which", "open", "python", "python3", "pip", "pip3", "node", "npm", "npx", "uv", "uvx", "bun",
                     "claude", "codex", "hokma", "bash", "sh", "zsh", "curl", "wget", "ssh", "sudo"}
_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com", ".py", ".sh")


class SkillInstallError(ValueError):
    """The install failed; the message says why and what to do, in plain words."""


class PythonInstallError(SkillInstallError):
    """The Python-package route failed after the repository was recognised as one (uv missing, uv or its step failed)."""


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
    opener = tls.opener(_GitHubOnly)
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


def python_package(folder: Path) -> dict[str, Any] | None:
    """{"name", "scripts"} when `folder` has a pyproject.toml naming a Python package, else None. It only reads;
    nothing from the repository runs here."""
    try:
        if _is_plain_file(folder / "pyproject.toml"):
            project = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
            name = project.get("name")
            if isinstance(name, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", name):
                return {"name": name, "scripts": sorted(project.get("scripts", {}))}
    except (OSError, ValueError):
        pass
    return None


def not_allowed_message(repo: str, name: str) -> str:
    return (f"{repo} is a Python package ({name}), not a skill, and Hokma only installs Python packages from a short list "
            "it trusts: installing any other would run the code of its build. Nothing was built or run. Follow its README "
            "yourself if you trust it.")


def installed_clashes(bin_dir: Path) -> list[str]:
    """The reserved command names among the files really in `bin_dir` (a .exe/.cmd/.bat suffix does not hide one)."""
    found: set[str] = set()
    try:
        entries = list(bin_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        low = entry.name.lower()
        for suffix in _EXE_SUFFIXES:
            if low.endswith(suffix):
                low = low[: -len(suffix)]
                break
        if low in RESERVED_COMMANDS:
            found.add(low)
    return sorted(found)


def _python_route(root: Path, start: Path, link: dict[str, str], package: dict[str, Any], runner=subprocess.run) -> dict[str, Any]:
    """A skill that is a Python package (graphify): installed with Hokma's own uv as a `uv tool`, its environment and
    command inside Hokma's data folder; then its setup step, if known, runs on that environment working on the
    working folder (which holds the projects). Never pip on Hokma's Python or the system's."""
    from hokma.core import tools
    from hokma.core.config import claude_config_dir

    repo, name = f"{link['owner']}/{link['repo']}", package["name"]
    allowed = PYPI_ALLOW.get(repo.lower())
    if not allowed:
        raise PythonInstallError(not_allowed_message(repo, name))
    uv = tools.uv_path()
    if not uv.is_file():
        raise PythonInstallError(f"{repo} is a Python package ({name}) and Hokma's uv is not installed ({uv}): "
                                "run `hokma install-tools` or `hokma update` and try again")
    tools.uv_tools_bin_dir().mkdir(parents=True, exist_ok=True)
    env = tools.with_tools(dict(os.environ))
    env.update({"UV_TOOL_DIR": str(tools.uv_tools_dir()), "UV_TOOL_BIN_DIR": str(tools.uv_tools_bin_dir()),
                "CLAUDE_CONFIG_DIR": str(claude_config_dir())})
    for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(var, None)

    def run(cmd: list[str], what: str) -> None:
        try:
            done = runner(cmd, env=env, cwd=str(root), capture_output=True, text=True, timeout=PY_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PythonInstallError(f"{what} failed: {exc}") from None
        if done.returncode != 0:
            tail = " ".join((done.stderr or done.stdout or "").split())[-400:]
            raise PythonInstallError(f"{what} failed ({done.returncode}): {tail}")

    run([str(uv), "tool", "install", "--force", f"{allowed['package']}=={allowed['version']}"],
        f"installing {name} with uv")
    clash = installed_clashes(tools.uv_tools_bin_dir())
    if clash:                                   # the real files that ended up in the bin, whatever declared them
        try:
            run([str(uv), "tool", "uninstall", allowed["package"]], f"removing {name}")
        except PythonInstallError:
            pass
        raise PythonInstallError(f"{repo} left the command(s) {', '.join(clash)}, which would shadow a tool of the system "
                                 "or Hokma: it was uninstalled and not installed")
    step = allowed["setup"]
    if step:
        run([str(tools.uv_tools_bin_dir() / tools._exe(step[0])), *step[1:]], f"the setup step `{' '.join(step)}`")
    notice = t("skill.notice.python_installed", package=name, repo=repo, folder=str(root))
    return {"kind": "python-skill", "repository": repo, "package": name, "commands": package["scripts"],
            "setup_step": " ".join(step) if step else None, "bin_dir": str(tools.uv_tools_bin_dir()),
            "workdir": str(root), "notice": notice, "note": "SHOW THIS NOTICE TO THE OWNER in your answer: " + notice}


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
        package = python_package(start) if not parsed["path"] else None
        if package and f"{parsed['owner']}/{parsed['repo']}".lower() in PYPI_ALLOW:
            return _python_route(root, start, parsed, package)
        if package and not folders:
            raise PythonInstallError(not_allowed_message(f"{parsed['owner']}/{parsed['repo']}", package["name"]))
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

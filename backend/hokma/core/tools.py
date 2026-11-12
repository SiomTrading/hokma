"""The runtimes Hokma puts next to uv and Python so plugins and their hooks can run: Node (LTS) and Bun.

Why: a plugin's hooks and installer scripts need Node or Bun (claude-mem starts its worker with Bun, its smart
installer runs on Node). A computer without them, which is the usual case, left plugins installed but dead. Hokma
installs both, portable, in `<data>/tools/node` and `<data>/tools/bun` (and `uvx` next to `uv`, which claude-mem needs
for its vector search), from the versions pinned in
`installer/lock.json` (`tools` entries with the sha256 of each archive; the archive is hashed BEFORE anything is
unpacked, and a wrong hash installs nothing).

The same code serves the three installers (`hokma install-tools`), `hokma update` (post-update) and `hokma doctor`.

`path_with_tools` puts these folders first on the PATH every engine process and terminal gets, so the hooks of
plugins (children of the engine) find `node`, `npm`, `npx`, `bun` and `uv` without any setup by the owner. At the end go
the places where plugin installers put what they download on their own (`~/.bun/bin`, `~/.local/bin`), so what a
plugin installs the first time it starts is on the PATH of its hooks too.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from hokma.core.config import hokma_data_dir
from hokma.core.pyhome import is_link

TOOLS = ("node", "bun")
MARKER = ".hokma-version"
CHUNK = 1024 * 1024
RETRIES = 3
TIMEOUT = 60

Report = Callable[[str], None]


class ToolsError(RuntimeError):
    pass


def tools_dir() -> Path:
    return hokma_data_dir() / "tools"


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def bin_dir(tool: str) -> Path:
    """The folder that goes on the PATH for `tool`: Node keeps its commands in `bin` (in the root on Windows), Bun is flat."""
    root = tools_dir() / tool
    return root / "bin" if tool == "node" and sys.platform != "win32" else root


def tool_path(tool: str) -> Path:
    return bin_dir(tool) / _exe(tool)


def _real_dir(path: Path) -> bool:
    """`path` is a folder we can walk. Any OSError (WinError 448 included) means it is not usable, never a crash:
    `Path.is_dir()` of Python 3.12 only ignores a few errors and would raise the rest."""
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def python_dirs() -> list[Path]:
    """The folders of the Python that uv installed for Hokma (`<data>/python/cpython-*`). The links uv makes to the
    minor version (`cpython-3.12-...`, a junction on Windows) are skipped: the real folder `cpython-3.12.x-...` is
    already in the list, and Windows can refuse to cross a link (WinError 448, WinError 448). A folder that cannot be
    read is left out; the others stay."""
    found: list[Path] = []
    try:
        roots = sorted((hokma_data_dir() / "python").glob("cpython-*"))
    except OSError:
        return []
    for root in roots:
        if is_link(root):
            continue
        found += [root / "Scripts", root] if sys.platform == "win32" else [root / "bin"]
    return [p for p in found if _real_dir(p)]


def tool_dirs() -> list[Path]:
    """Hokma's own folders (uv, Node, Bun, Python), which go in front of the PATH."""
    return [hokma_data_dir() / "bin", bin_dir("node"), bin_dir("bun"), *python_dirs()]


def installer_dirs() -> list[Path]:
    """Where plugin installers put what they download on their own, which go at the end of the PATH."""
    home = Path.home()
    bun_install = os.environ.get("BUN_INSTALL")
    return [Path(bun_install) / "bin" if bun_install else home / ".bun" / "bin", home / ".local" / "bin"]


def path_with_tools(path: str | None, *, first: bool = True, only_existing: bool = False) -> str:
    """`path` with Hokma's folders in front (or at the end with `first=False`) and the installers' folders at the end,
    each once."""
    ours, last = [str(p) for p in tool_dirs()], [str(p) for p in installer_dirs()]
    if only_existing:                      # the owner's shell: a folder that is not there is noise (and PATH has a size limit)
        ours, last = [p for p in ours if os.path.isdir(p)], [p for p in last if os.path.isdir(p)]
    middle = [p for p in (path or "").split(os.pathsep) if p and p not in ours]
    return os.pathsep.join([*ours, *middle, *[p for p in last if p not in middle]] if first
                           else [*middle, *ours, *[p for p in last if p not in middle and p not in ours]])


def with_tools(env: dict[str, str], *, first: bool = True, only_existing: bool = False) -> dict[str, str]:
    """`env` with `path_with_tools` applied to its PATH variable (any letter case: `Path` on Windows)."""
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[key] = path_with_tools(env.get(key), first=first, only_existing=only_existing)
    return env


# ------------------------------------------------------------------------------------------------ the lock

def lock_path() -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "installer" / "lock.json"


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64" if machine in ("x86_64", "amd64") else machine
    system = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    return f"{system}-{arch}"


def load_lock(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or lock_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolsError(f"cannot read lock.json: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise ToolsError("lock.json has no tools")
    return data


def entry(lock: dict, tool: str) -> dict:
    found = lock["tools"].get(f"{tool}-{platform_key()}")
    version = lock.get(f"{tool}_version")
    if not isinstance(found, dict) or not found.get("url") or not found.get("sha256") or not version:
        raise ToolsError(f"lock.json has no {tool} for {platform_key()}")
    return {**found, "version": str(version)}


# -------------------------------------------------------------------------------------------- download, unpack

def download(url: str, dest: Path) -> str:
    """Download `url` to `dest` over HTTPS and return its sha256."""
    if not url.startswith("https://"):
        raise ToolsError(f"not an https address: {url}")
    last: Exception | None = None
    for _ in range(RETRIES):
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response, open(dest, "wb") as out:   # noqa: S310
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    out.write(block)
            return digest.hexdigest()
        except OSError as exc:
            last = exc
    raise ToolsError(f"download failed ({url}): {last}")


def _safe_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or ".." in parts or (len(parts[0]) == 2 and parts[0][1] == ":"))


def unpack(archive: Path, dest: Path) -> None:
    """Unpack a .zip or .tar.gz into `dest`, dropping the one top folder the release puts everything in."""
    dest.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if n and not n.endswith("/")]
            if not all(_safe_name(n) for n in names):
                raise ToolsError("the archive has an unsafe path")
            top = {n.split("/", 1)[0] for n in names}
            strip = len(top) == 1 and all("/" in n for n in names)
            for n in names:
                target = dest / (n.split("/", 1)[1] if strip else n)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if sys.platform != "win32":
                    target.chmod(0o755 if target.name in ("bun", "node", "npm", "npx", "corepack") else 0o644)
        return
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not all(_safe_name(m.name) for m in members):
            raise ToolsError("the archive has an unsafe path")
        top = {m.name.split("/", 1)[0] for m in members}
        strip = len(top) == 1
        for m in members:
            if strip:
                if "/" not in m.name:
                    continue
                m.name = m.name.split("/", 1)[1]
            tar.extract(m, dest, filter="data")       # nothing leaves `dest`; the executable bit is kept (node needs it)


def installed_version(tool: str) -> str | None:
    try:
        return (tools_dir() / tool / MARKER).read_text(encoding="utf-8").strip() if tool_path(tool).is_file() else None
    except OSError:
        return None


def install_tool(tool: str, lock: dict, report: Report = lambda _: None,
                 fetch: Callable[[str, Path], str] = download) -> bool:
    """Install `tool` at the version of the lock unless it is already there. True when something was installed."""
    found = entry(lock, tool)
    if installed_version(tool) == found["version"]:
        return False
    work_root = hokma_data_dir() / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{tool}.", dir=work_root))
    try:
        report(f"downloading {tool} {found['version']}")
        archive = work / found["url"].rsplit("/", 1)[-1]
        actual = fetch(found["url"], archive)
        if actual.lower() != str(found["sha256"]).lower():
            raise ToolsError(f"{tool}: sha256 mismatch (expected {found['sha256']}, got {actual})")
        staged = work / "staged"
        unpack(archive, staged)
        if not (staged / ("bin" if tool == "node" and sys.platform != "win32" else ".") / _exe(tool)).is_file():
            raise ToolsError(f"{tool}: the archive has no {_exe(tool)}")
        (staged / MARKER).write_text(found["version"] + "\n", encoding="utf-8")
        target = tools_dir() / tool
        tools_dir().mkdir(parents=True, exist_ok=True)
        old = work / "old"
        if target.exists():
            target.rename(old)                     # a running node.exe cannot be deleted on Windows; it is moved aside
        staged.rename(target)
        report(f"{tool} {found['version']} installed")
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def uvx_path() -> Path:
    return hokma_data_dir() / "bin" / _exe("uvx")


def install_uvx(lock: dict, report: Report = lambda _: None, fetch: Callable[[str, Path], str] = download) -> bool:
    """`uvx` (it runs Python tools such as chroma-mcp, which claude-mem starts) comes in the same archive as `uv`, but the
    installers copied only `uv`. When `uv` is there and `uvx` is not, take `uvx` from the archive of the lock (sha256
    checked before unpacking)."""
    found = lock.get("platforms", {}).get(platform_key())
    if uvx_path().is_file() or not (hokma_data_dir() / "bin" / _exe("uv")).is_file():
        return False
    if not isinstance(found, dict) or not found.get("url") or not found.get("sha256"):
        raise ToolsError(f"lock.json has no uv for {platform_key()}")
    work_root = hokma_data_dir() / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="uvx.", dir=work_root))
    try:
        report("downloading uvx")
        archive = work / found["url"].rsplit("/", 1)[-1]
        actual = fetch(found["url"], archive)
        if actual.lower() != str(found["sha256"]).lower():
            raise ToolsError(f"uvx: sha256 mismatch (expected {found['sha256']}, got {actual})")
        staged = work / "staged"
        unpack(archive, staged)
        source = staged / _exe("uvx")
        if not source.is_file():
            raise ToolsError("uvx: the archive has no uvx")
        part = uvx_path().with_name(uvx_path().name + ".part")
        shutil.copy2(source, part)
        part.chmod(0o755)
        os.replace(part, uvx_path())
        report("uvx installed")
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure_tools(report: Report = lambda _: None, lock: dict | None = None,
                 fetch: Callable[[str, Path], str] = download) -> list[str]:
    """Install Node and Bun when they are missing or at another version than the lock, and `uvx` next to `uv`. Returns
    what was installed."""
    lock = lock or load_lock()
    done = [tool for tool in TOOLS if install_tool(tool, lock, report, fetch)]
    if install_uvx(lock, report, fetch):
        done.append("uvx")
    return done

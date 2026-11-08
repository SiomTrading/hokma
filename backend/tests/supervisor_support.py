"""Helpers for the supervisor and restart tests: a fake installation with two runtime slots, fake releases, and
/proc scanning (Linux). No network and no dependency installer: `uv` is replaced by a small script that builds a
real virtual environment whose site-packages point at the slot's own copy of the code."""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE_VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "backend" / "hokma" / "__init__.py").read_text()).group(1)
APP_PARTS = ("backend", "cli", "kit", "dashboard/dist", "installer/lock.json", "pyproject.toml", "uv.lock", "CHANGELOG.md")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache", "node_modules", ".venv")

STUB_UV = '''#!{python}
import os, pathlib, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["python", "install"]:
    sys.exit(0)
if args and args[0] == "sync":
    venv = pathlib.Path(os.environ["UV_PROJECT_ENVIRONMENT"])
    app = pathlib.Path(args[args.index("--project") + 1])
    subprocess.check_call([{base!r}, "-m", "venv", "--without-pip", str(venv)])
    site = next((venv / "lib").glob("python*/site-packages"))
    (site / "slot.pth").write_text(f"{{app}}/backend\\n{{app}}/cli\\nimport site; site.addsitedir({deps!r})\\n")
    exe = venv / "bin" / "hokma"
    exe.write_text(f'#!/bin/sh\\nexec "{{venv}}/bin/python" -P -m hokma_cli.main "$@"\\n')   # -P: like a real console script, the cwd is not on sys.path
    exe.chmod(0o755)
    sys.exit(0)
sys.exit(2)
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_app(dest: Path, version: str | None = None) -> None:
    for part in APP_PARTS:
        src = ROOT / part
        target = dest / part
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=IGNORE)
        else:
            shutil.copy2(src, target)
    if version:
        init = dest / "backend" / "hokma" / "__init__.py"
        init.write_text(init.read_text().replace(f'"{BASE_VERSION}"', f'"{version}"'))


class Install:
    """A Linux/macOS style installation in a temporary folder, running version BASE_VERSION from slot a."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.work = root / "work"
        self.home = root / "home"
        self.manifest = root / "manifest.json"
        for d in (self.data / "bin", self.work, self.home):
            d.mkdir(parents=True)
        uv = self.data / "bin" / "uv"
        deps = sysconfig.get_paths()["purelib"]
        base = getattr(sys, "_base_executable", sys.executable)
        uv.write_text(STUB_UV.format(python=sys.executable, base=base, deps=deps))
        uv.chmod(0o755)
        self._build_slot("a", None)
        (self.data / "current").write_text("a\n")
        self.exe_a = self.exe("a")

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG")) and k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(HOME=str(self.home), HOKMA_HOME=str(self.work), HOKMA_DATA_DIR=str(self.data), HOKMA_LANG="en",
                   HOKMA_UPDATE_SOURCE=str(self.manifest), **extra)
        return env

    def exe(self, slot: str) -> Path:
        return self.data / "runtime" / slot / "venv" / "bin" / "hokma"

    def slot_dir(self, slot: str) -> Path:
        return self.data / "runtime" / slot

    def current(self) -> str:
        return (self.data / "current").read_text().strip()

    def _build_slot(self, slot: str, version: str | None) -> None:
        slot_dir = self.slot_dir(slot)
        copy_app(slot_dir / "app", version)
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(slot_dir / "venv")}
        subprocess.run([str(self.data / "bin" / "uv"), "sync", "--project", str(slot_dir / "app")], env=env, check=True)
        for step in ("write-manifest", "verify-install"):
            subprocess.run([str(self.exe(slot)), step], env=self.env(), check=True, capture_output=True)

    def publish(self, version: str, *, bad_sha: bool = False) -> None:
        """Write a release package and point the update source at it."""
        src = self.root / f"release-{version}" / f"hokma-{version}"
        copy_app(src, version)
        pkg = self.root / f"hokma-{version}.tar.gz"
        with tarfile.open(pkg, "w:gz") as tar:
            tar.add(src, arcname=src.name)
        sha = "0" * 64 if bad_sha else hashlib.sha256(pkg.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps({"version": version, "url": pkg.name, "sha256": sha}))


def http_json(url: str, token: str | None = None, method: str = "GET", timeout: float = 120.0):
    req = urllib.request.Request(url, method=method, headers={"X-Hokma-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def health(port: int, timeout: float = 1.0) -> dict | None:
    try:
        return http_json(f"http://127.0.0.1:{port}/health", timeout=timeout)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def wait_until(predicate, seconds: float, interval: float = 0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def session_pids(sid: int) -> list[int]:
    """Every live process of a session (Linux)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[3]) == sid and fields[0] != "Z":          # session id; zombies do not count
                found.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return found


def children_of(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
                if int(stat[stat.rindex(")") + 2:].split()[1]) == pid:
                    out.append(int(entry.name))
            except (OSError, ValueError):
                continue
    return out


def cmdline(pid: int) -> list[str]:
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def find_engine(sid: int) -> int | None:
    for pid in session_pids(sid):
        if "engine" in cmdline(pid):
            return pid
    return None


def holds_path(pid: int, folder: Path) -> list[str]:
    """What a process keeps open or mapped under `folder` (cwd, exe, descriptors, memory maps)."""
    base, hits = str(folder.resolve()), []
    proc = Path("/proc") / str(pid)
    for name in ("cwd", "exe"):
        try:
            if os.readlink(proc / name).startswith(base):
                hits.append(name)
        except OSError:
            pass
    try:
        for fd in (proc / "fd").iterdir():
            try:
                if os.readlink(fd).startswith(base):
                    hits.append(f"fd {fd.name}")
            except OSError:
                pass
        for line in (proc / "maps").read_text().splitlines():
            if base in line:
                hits.append("map " + line.split()[-1])
    except OSError:
        pass
    return hits


def token_in_process(pid: int, token: str) -> list[str]:
    """Where a token shows up in a process: arguments or environment (Linux)."""
    needle = token.encode()
    hits = []
    for name in ("cmdline", "environ"):
        try:
            if needle in (Path("/proc") / str(pid) / name).read_bytes():
                hits.append(name)
        except OSError:
            pass
    return hits


def token_in_files(token: str, *folders: Path) -> list[Path]:
    needle = token.encode()
    hits = []
    for folder in folders:
        for path in folder.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink() and needle in path.read_bytes():
                    hits.append(path)
            except OSError:
                continue
    return hits

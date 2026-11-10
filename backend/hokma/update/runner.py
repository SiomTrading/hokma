"""`hokma update`: replace only the runtime, atomically, never touching the student's work.

Blue-green: the new version is built in the INACTIVE slot (runtime/a or runtime/b) at its final path
(a venv cannot be moved), verified, and only then does the one-line `current` pointer flip with an atomic
file replace. Any failure before the flip leaves the pointer, and so the previous version, untouched; a
failure after the flip (kit/settings step) flips the pointer back.

Never touched: the working folder (except additive kit commits, see hokma.kit.sync) and claude-config
(except regenerating settings.json).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from hokma import __version__
from hokma.core.proc import spawn_kwargs
from hokma.core.config import (current_pointer, current_slot, hokma_data_dir, hokma_home, other_slot,
                               running_slot, slot_dir, slot_exe)
from hokma.i18n import t
from hokma.update.archive import extract, fetch_archive
from hokma.update.source import ManifestSource, UpdateError, UpdateSource, parse_version

LOCK_STALE_SECONDS = 30 * 60
REMOVE_ATTEMPTS = 5
REMOVE_WAIT_SECONDS = 1.0
VERSION_LINE = re.compile(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"', re.M)


@dataclass
class UpdateResult:
    status: str                      # "updated" | "up-to-date"
    from_version: str
    to_version: str
    slot: str | None = None
    restart_required: bool = False
    kit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _exe(slot: str) -> Path:
    return slot_exe(slot)


def _uv() -> Path:
    return hokma_data_dir() / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HOKMA_HOME=str(hokma_home()), HOKMA_DATA_DIR=str(hokma_data_dir()))
    return env


def _run(cmd: list[str], what: str, env: dict[str, str], timeout: int = 900) -> str:
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           **spawn_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"{what}: {exc}") from exc
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise UpdateError(t("update.step_failed", step=what, detail=" | ".join(tail)))
    return r.stdout


def _remove_slot(path: Path) -> None:
    """Delete the inactive slot. On Windows a file in use (a leftover process, a scanner) cannot be deleted: wait and
    retry, and if it stays locked say so in plain words instead of a traceback."""
    for attempt in range(1, REMOVE_ATTEMPTS + 1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == REMOVE_ATTEMPTS:
                raise UpdateError(t("update.slot_busy", path=path)) from exc
            time.sleep(REMOVE_WAIT_SECONDS)


def write_pointer(slot: str) -> None:
    """Atomic switch: write a temp file, fsync, then replace `current`."""
    pointer = current_pointer()
    tmp = pointer.with_name("current.tmp")
    with open(tmp, "w", encoding="ascii") as f:
        f.write(slot + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, pointer)


class _Lock:
    def __init__(self) -> None:
        self.path = hokma_data_dir() / "update.lock"

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
            self.path.unlink(missing_ok=True)   # left by a crashed update
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise UpdateError(t("update.in_progress")) from exc
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def run_update(source: UpdateSource | str, *, force: bool = False,
               progress: Callable[[str], None] | None = None) -> UpdateResult:
    """`progress(stage)` is called when each real stage starts: download, unpack, python, dependencies,
    verify, switch."""
    step = progress or (lambda _stage: None)
    if isinstance(source, str):
        source = ManifestSource(source)
    active = current_slot()
    if active is None:
        raise UpdateError(t("update.not_installed"))

    manifest = source.fetch()
    if parse_version(manifest.version) == parse_version(__version__) and not force:
        return UpdateResult("up-to-date", __version__, manifest.version)
    if parse_version(manifest.version) < parse_version(__version__) and not force:
        raise UpdateError(t("update.older", new=manifest.version, current=__version__))

    target = other_slot(active)
    if running_slot() == target:
        # This process lives in the slot that the update must rebuild (it kept running after an earlier update
        # moved `current`). Its files are in use: never try to delete them.
        raise UpdateError(t("update.running_old_slot"))

    with _Lock():
        tmp_root = hokma_data_dir() / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="update-", dir=tmp_root))
        target_dir = slot_dir(target)
        flipped = False
        try:
            archive = work / "release.pkg"
            step("download")
            fetch_archive(manifest.url, archive, manifest.sha256, manifest.headers)        # hash checked before unpacking
            step("unpack")
            release = extract(archive, work / "release")

            declared = VERSION_LINE.search((release / "backend/hokma/__init__.py").read_text(encoding="utf-8"))
            if not declared or declared.group(1) != manifest.version:
                raise UpdateError(t("update.version_mismatch"))
            try:
                py_version = json.loads((release / "installer/lock.json").read_text(encoding="utf-8"))["python_version"]
            except (OSError, ValueError, KeyError) as exc:
                raise UpdateError(t("update.no_lock")) from exc
            if not _uv().is_file():
                raise UpdateError(t("update.no_uv", path=_uv()))

            # Build the new version in the inactive slot, at its final path.
            if target_dir.exists():
                _remove_slot(target_dir)           # leftovers from an earlier failed or interrupted update
            (target_dir).mkdir(parents=True)
            shutil.copytree(release, target_dir / "app", symlinks=False)
            env = _env()
            env.update(UV_NO_CONFIG="1", UV_PYTHON_INSTALL_DIR=str(hokma_data_dir() / "python"),
                       UV_CACHE_DIR=str(hokma_data_dir() / "cache"), UV_PYTHON_PREFERENCE="only-managed",
                       UV_PROJECT_ENVIRONMENT=str(target_dir / "venv"))
            step("python")
            _run([str(_uv()), "python", "install", py_version], t("update.step.python"), env)
            step("dependencies")
            _run([str(_uv()), "sync", "--project", str(target_dir / "app"), "--frozen", "--no-dev",
                  "--python", py_version], t("update.step.dependencies"), env)

            new = str(_exe(target))
            step("verify")
            _run([new, "write-manifest"], t("update.step.manifest"), _env())
            _run([new, "verify-install"], t("update.step.verify"), _env())
            said = _run([new, "version"], t("update.step.smoke"), _env()).strip()
            if said != f"hokma {manifest.version}":
                raise UpdateError(t("update.version_said", said=repr(said), expected=manifest.version))

            step("switch")
            write_pointer(target)                   # THE atomic switch
            flipped = True
            out = _run([new, "post-update"], t("update.step.post"), _env())
            kit = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except BaseException:
            if flipped:
                write_pointer(active)               # roll back to the previous version
                try:
                    from hokma.kit.settings import write_settings
                    write_settings()
                except Exception:
                    pass
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return UpdateResult("updated", __version__, manifest.version, slot=target, restart_required=True, kit=kit)



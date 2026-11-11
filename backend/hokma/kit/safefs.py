"""Symlink-safe reads and writes inside the student's working folder.

The agent can create symlinks in the working folder. Hokma writes there (kit files, mirror of the
extensions), so every write must prove the target and every parent folder are plain files and folders
inside the root, and must not be redirected between the check and the write.

POSIX: walk the path one component at a time with O_NOFOLLOW file descriptors (no check-then-use race),
write a temporary file in the same folder and `os.replace` it with dir_fd.
Windows (no dir_fd): lstat every component, write a temporary file in the same folder, check that the
realpath of the folder is still inside the root, then `os.replace`. This variant has a small race window.
"""
import os
import stat
import time
import uuid
from pathlib import Path

from hokma.i18n import t


class UnsafePath(Exception):
    """A symlink, a non-regular file or a path that leaves the root."""


_POSIX = os.name == "posix" and os.replace in os.supports_dir_fd and os.open in os.supports_dir_fd


def _parts(rel: str) -> tuple[str, ...]:
    p = Path(rel)
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise UnsafePath(t("fs.path_not_allowed", path=repr(rel)))
    return p.parts


# ---- POSIX --------------------------------------------------------------------------------

def _open_dir(fd: int, name: str, create: bool) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, flags, dir_fd=fd)
        except OSError as exc:
            raise UnsafePath(t("fs.unsafe_dir", name=name)) from exc
    except OSError as exc:      # ELOOP (symlink) or ENOTDIR
        raise UnsafePath(t("fs.unsafe_dir_link", name=name)) from exc


def _walk_posix(root: Path, parts: tuple[str, ...], create: bool) -> tuple[list[int], int | None]:
    fds = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for part in parts[:-1]:
            nfd = _open_dir(fds[-1], part, create)
            if nfd is None:
                return fds, None
            fds.append(nfd)
        return fds, fds[-1]
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def _read_posix(root: Path, rel: str, max_bytes: int | None = None, single_link: bool = False) -> bytes | None:
    parts = _parts(rel)
    fds, dfd = _walk_posix(root, parts, create=False)
    try:
        if dfd is None:
            return None
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)   # a FIFO must not hang the open
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnsafePath(t("fs.unsafe_file_link", path=rel)) from exc
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink > 1):
                raise UnsafePath(t("fs.not_regular", path=rel))      # checked on the descriptor that is read
            data = f.read() if max_bytes is None else f.read(max_bytes + 1)
            if max_bytes is not None and len(data) > max_bytes:
                raise UnsafePath(t("fs.not_regular", path=rel))
            return data
    finally:
        for fd in fds:
            os.close(fd)


def _write_posix(root: Path, rel: str, data: bytes, mode: int) -> None:
    parts = _parts(rel)
    name = parts[-1]
    fds, dfd = _walk_posix(root, parts, create=True)
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(t("fs.target_not_regular", path=rel))
        except FileNotFoundError:
            pass
        tmp = f".{name}.hokma-tmp-{os.getpid()}"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(tfd, "wb") as f:
                f.write(data)
                f.flush()
                os.fchmod(f.fileno(), mode)
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        for fd in fds:
            os.close(fd)


# ---- generic (Windows) --------------------------------------------------------------------

def _chain_generic(root: Path, parts: tuple[str, ...]) -> str:
    """'missing' | 'file' | 'unsafe', by lstat of every component."""
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(st.st_mode):
            return "unsafe"
        last = i == len(parts) - 1
        if last:
            return "file" if stat.S_ISREG(st.st_mode) else "unsafe"
        if not stat.S_ISDIR(st.st_mode):
            return "unsafe"
    return "missing"


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def _read_generic(root: Path, rel: str, max_bytes: int | None = None, single_link: bool = False) -> bytes | None:
    state = _chain_generic(root, _parts(rel))
    if state == "missing":
        return None
    if state == "unsafe":
        raise UnsafePath(t("fs.unsafe_path", path=rel))
    path = root / rel
    if single_link and path.stat().st_nlink > 1:
        raise UnsafePath(t("fs.not_regular", path=rel))
    with open(path, "rb") as f:
        data = f.read() if max_bytes is None else f.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise UnsafePath(t("fs.not_regular", path=rel))
    return data


# OneDrive, antivirus and the search indexer open files the instant they appear, so on Windows a write that is
# correct can fail once (the folder is briefly gone, the temp file is locked). Retry with a fresh temp name.
WRITE_ATTEMPTS = 6
WRITE_BACKOFF = 0.2          # seconds, multiplied by the attempt number


def _transient(exc: OSError) -> bool:
    """Missing file or folder, access denied or sharing violation: what a sync client or scanner causes."""
    return isinstance(exc, (FileNotFoundError, PermissionError)) or getattr(exc, "winerror", None) in (5, 32, 33)


def _write_generic_once(root: Path, rel: str, data: bytes, mode: int, tmp_name: str) -> None:
    parts = _parts(rel)
    cur = root
    for part in parts[:-1]:
        cur = cur / part
        if not os.path.islink(cur):
            try:
                cur.mkdir()
            except FileExistsError:
                pass
    if _chain_generic(root, parts) == "unsafe":
        raise UnsafePath(t("fs.unsafe_path", path=rel))
    dst = root / rel
    tmp = dst.with_name(tmp_name)
    try:
        tmp.write_bytes(data)
        if not _inside(tmp.parent, root) or os.path.islink(tmp):
            raise UnsafePath(t("fs.escapes", path=rel))
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_generic(root: Path, rel: str, data: bytes, mode: int) -> None:
    name = _parts(rel)[-1]
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        tmp_name = f".{name}.hokma-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            return _write_generic_once(root, rel, data, mode, tmp_name)
        except UnsafePath:
            raise
        except OSError as exc:
            if not _transient(exc) or attempt == WRITE_ATTEMPTS:
                raise
            time.sleep(WRITE_BACKOFF * attempt)


# ---- public API ---------------------------------------------------------------------------

def read_regular(root: Path, rel: str, *, max_bytes: int | None = None, single_link: bool = False) -> bytes | None:
    """Bytes of a plain file under root, None if missing; UnsafePath on symlinks or odd files, and, when asked, on a
    file bigger than `max_bytes` (never read whole) or with more than one hard link (checked on the open descriptor)."""
    if _POSIX:
        return _read_posix(root, rel, max_bytes, single_link)
    return _read_generic(root, rel, max_bytes, single_link)


def write_atomic(root: Path, rel: str, data: bytes, mode: int = 0o644) -> None:
    """Create or replace a plain file under root without ever following a symlink."""
    if _POSIX:
        _write_posix(root, rel, data, mode)
    else:
        _write_generic(root, rel, data, mode)

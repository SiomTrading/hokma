"""Consistent copies of the SQLite database: a WAL-safe snapshot, an integrity check and a safe restore."""
import logging
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


def _query_ro_sqlite(path: Path, fn):
    """Run ``fn(conn)`` on a read-only connection to *path*; return ``(value, None)`` or ``(None, exc)``."""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        return fn(conn), None
    except Exception as exc:
        return None, exc
    finally:
        _close_quietly(conn)


def safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    """
    conn = backup_conn = None
    try:
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=0.0)
        backup_conn = sqlite3.connect(str(dst))
        busy_deadline = time.monotonic() + max(0.0, timeout_seconds)

        def _check_backup_progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_deadline
            now = time.monotonic()
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)

        conn.backup(backup_conn, pages=256, progress=_check_backup_progress, sleep=0.1)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        _close_quietly(backup_conn)
        _close_quietly(conn)


_SQLITE_HEADER = b"SQLite format 3\0"

# Above this size PRAGMA integrity_check (it walks every page) is replaced by an O(1) header and schema probe.
DEFAULT_INTEGRITY_CHECK_MAX_BYTES = 2 << 30  # 2 GiB


def _read_header(path: Path, length: int) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read(length)
    except OSError:
        return None


def verify_sqlite_integrity(
    path: Path, *, check_header: bool = True, run_pragma: bool = True,
    max_bytes: int = DEFAULT_INTEGRITY_CHECK_MAX_BYTES) -> dict:
    """Verify a SQLite database: existence + minimum size, header magic, then a read-only
    ``PRAGMA integrity_check`` (or a cheap structural probe above ``max_bytes``)."""
    def _done(message: str, valid: bool = False, size: Optional[int] = None) -> dict:
        return {"valid": valid, "message": message, "size": size}
    try:
        st = path.stat()
    except FileNotFoundError:
        return _done(f"not found: {path}")
    except OSError as exc:
        return _done(f"cannot stat: {exc}")
    size = st.st_size
    if size < 100:  # SQLite minimum viable size (header + 1 page)
        return _done(f"too small ({size} bytes) to be a valid SQLite database", size=size)
    if check_header:
        head = _read_header(path, len(_SQLITE_HEADER))
        if head is None:
            return _done("cannot read header", size=size)
        if head != _SQLITE_HEADER:
            return _done(f"missing SQLite header magic (got {head[:16].hex()!r})", size=size)
    if max_bytes > 0 and size > max_bytes:
        # O(1) probe: the header check caught the zeroed signature; reading sqlite_master + page
        # geometry catches malformed-schema and truncated-header-page classes without a data walk.
        _, exc = _query_ro_sqlite(path, lambda c: (
            c.execute("PRAGMA schema_version").fetchone(),
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()))
        if exc is not None:
            kind = "failed" if isinstance(exc, sqlite3.DatabaseError) else "error"
            return _done(f"schema probe {kind}: {exc}", size=size)
        return _done(
            f"size {size:,} bytes exceeds max_bytes {max_bytes:,}; "
            "skipped PRAGMA integrity_check (header + schema probe passed)",
            valid=True, size=size)
    if run_pragma:
        rows, exc = _query_ro_sqlite(
            path, lambda c: [str(r[0]) for r in c.execute("PRAGMA integrity_check")])
        if exc is not None:
            kind = "cannot open database" if isinstance(exc, sqlite3.DatabaseError) else "integrity check error"
            return _done(f"{kind}: {exc}", size=size)
        if rows == ["ok"]:
            return _done("integrity check passed", valid=True, size=size)
        return _done(f"integrity check failed: {'; '.join(rows[:5])}", size=size)
    return _done("header check passed", valid=True, size=size)


def _foreign_db_holder_pids(db_path: Path) -> Optional[List[int]]:
    """PIDs of OTHER processes holding *db_path* or its WAL/SHM open (Linux ``/proc`` scan).

    An already-unlinked ``(deleted)`` sidecar, the split-brain fingerprint, still
    counts as held. None off-Linux or when /proc fails.
    """
    if not sys.platform.startswith("linux"):
        return None

    def _canonical(path: str) -> str:
        return os.path.normcase(os.path.abspath(path.removesuffix(" (deleted)")))

    def _holds_watched(fds: List[str], fd_dir: str) -> bool:
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if _canonical(target) in watched:
                return True
        return False

    canonical_db = _canonical(os.fspath(db_path))
    watched = {canonical_db, canonical_db + "-wal", canonical_db + "-shm"}
    pids: List[int] = []
    try:
        own_pid = os.getpid()
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit() or int(pid_str) == own_pid:
                continue
            fd_dir = f"/proc/{pid_str}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            if _holds_watched(fds, fd_dir):
                pids.append(int(pid_str))
    except OSError:
        return None
    return pids


def safe_restore_db(src: Path, dst: Path) -> bool:
    """Restore snapshot *src* into live *dst* through the backup() API; unlink+move fallback.

    Writing pages into the live file preserves its inode and WAL state, so other holders (the
    dashboard, another process) see the restored data instead of stale pages from a replaced inode.
    The fallback runs ONLY when no other process holds the file
    (replacing the inode under a live holder is a split-brain); otherwise it fails closed
    (``False``) and the caller reports the file as skipped.
    """
    try:
        dst_conn = sqlite3.connect(str(dst))
        # Checkpoint first so the backup starts clean rather than writing on top of a deep WAL.
        with suppress(Exception):
            dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as src_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        with suppress(Exception):
            dst.chmod(src.stat().st_mode)
        return True
    except Exception as exc:
        logger.warning("SQLite safe restore failed for %s -> %s: %s", src, dst, exc)
        return _unlink_move_restore_db(src, dst)


def _unlink_move_restore_db(src: Path, dst: Path) -> bool:
    """Fallback restore: unlink+move. Only safe when no process holds the DB open.

    Replacing the inode under a live holder corrupts it (the holder keeps writing through a
    deleted-inode fd and loses its WAL index), so fail closed. The caller closes its own
    connections to *dst* before restoring.
    """
    try:
        holders = _foreign_db_holder_pids(dst)
        if holders:
            logger.error("Refusing unlink+move restore of %s: process(es) %s still "
                         "hold the database or its WAL open. Stop them and retry.", dst, holders)
            return False
        tmp = dst.parent / f".{dst.name}.snap_restore"
        shutil.copy2(src, tmp)
        dst.unlink(missing_ok=True)
        # The snapshot owns no WAL, so any -wal/-shm here belongs to the DB just unlinked (a
        # killed process leaves them, exactly when a restore runs); SQLite would replay that
        # foreign WAL over the restored file: "malformed" or resurrected post-snapshot rows.
        for _sidecar_suffix in ("-wal", "-shm", "-journal"):
            dst.with_name(dst.name + _sidecar_suffix).unlink(missing_ok=True)
        shutil.move(str(tmp), str(dst))
        return True
    except Exception as exc2:
        logger.error("Fallback restore also failed for %s -> %s: %s", src, dst, exc2)
        return False

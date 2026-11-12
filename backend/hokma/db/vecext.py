"""Loading of the `sqlite-vec` extension into a SQLite connection, and the check that it can be loaded.

Kept free of other Hokma imports so both the SQLite session (`backend.py`) and the vector store (`vectors.py`) can
use it. The check is the same smoke test the CI runs on every platform: load the extension, create a `vec0` table,
insert two vectors and ask for the nearest one.
"""
from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass

NO_LOAD_EXTENSION = "no_load_extension"      # this Python cannot load SQLite extensions at all: no fix by retrying
MODULE_MISSING = "module_missing"            # the `sqlite_vec` package is not installed: no fix by retrying
LOAD_FAILED = "load_failed"                  # the library exists but did not load (blocked file, wrong build): retry


@dataclass(frozen=True)
class VecSupport:
    ok: bool
    code: str | None = None
    detail: str = ""
    retry: bool = False


def _load(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def load_into(conn: sqlite3.Connection) -> bool:
    """Load the extension into `conn`; False (never an exception) when it is not available."""
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        _load(conn)
        return True
    except Exception:                                                  # noqa: BLE001  (a missing or blocked library)
        return False


def check_support() -> VecSupport:
    """Whether this Python can load `sqlite-vec` and run a nearest-vector query with it."""
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return VecSupport(False, NO_LOAD_EXTENSION, "sqlite3.Connection has no enable_load_extension")
    try:
        import sqlite_vec  # noqa: F401
    except ImportError as exc:
        return VecSupport(False, MODULE_MISSING, str(exc))
    conn = sqlite3.connect(":memory:")
    try:
        _load(conn)
        conn.execute("CREATE VIRTUAL TABLE smoke USING vec0(embedding float[2])")
        for rowid, vector in ((1, (1.0, 0.0)), (2, (0.0, 1.0))):
            conn.execute("INSERT INTO smoke(rowid, embedding) VALUES (?, ?)", (rowid, struct.pack("2f", *vector)))
        row = conn.execute("SELECT rowid FROM smoke WHERE embedding MATCH ? AND k = 1",
                           (struct.pack("2f", 0.9, 0.1),)).fetchone()
        if row is None or row[0] != 1:
            return VecSupport(False, LOAD_FAILED, "the nearest-vector smoke query gave a wrong answer", True)
        return VecSupport(True)
    except Exception as exc:                                           # noqa: BLE001
        return VecSupport(False, LOAD_FAILED, f"{type(exc).__name__}: {exc}", True)
    finally:
        conn.close()

"""Versioned, idempotent migrations.

`backend/hokma/db/migrations/NNNN_name.sql` files are applied in order, one transaction per file, and recorded in
`schema_version`. An existing database is copied to `<data>/backups/` before anything changes. A failing file is
rolled back and the copy stays. Safe to call on every start: when nothing is pending it only runs the seeds, and
the seeds change nothing that is already right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from hokma.db.backend import Backend, SqliteBackend
from hokma.db.errors import DbError, MigrationError
from hokma.db.paths import backups_dir, db_path
from hokma.db.timeutil import utcnow
from hokma.i18n import t

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_ENGINE = re.compile(r"^--\s*@engine:\s*([a-z]+)\s*$")
KEEP_BACKUPS = 10


@dataclass
class MigrationResult:
    applied: list[int] = field(default_factory=list)
    version: int = 0
    backup: Path | None = None
    created: bool = False                      # the database file did not exist before


def discover(directory: Path | None = None) -> list[tuple[int, str, Path]]:
    """(version, name, path) of every migration file, ordered; versions must be unique."""
    found: list[tuple[int, str, Path]] = []
    for path in sorted((directory or MIGRATIONS_DIR).glob("*.sql")):
        match = _FILE.match(path.name)
        if not match:
            raise MigrationError(t("db.migrate.bad_name", file=path.name))
        found.append((int(match.group(1)), match.group(2), path))
    versions = [v for v, _, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(t("db.migrate.duplicate"))
    return found


def render(text: str, backend: Backend) -> str:
    """Keep the sections that apply to this engine and fill the {PK} and {TS} marks."""
    current, kept = "all", []
    for line in text.splitlines():
        match = _ENGINE.match(line.strip())
        if match:
            current = match.group(1)
            continue
        if current in ("all", backend.engine):
            kept.append(line)
    script = "\n".join(kept)
    for mark, replacement in backend.marks.items():
        script = script.replace(mark, replacement)
    return script


def _version_of(session) -> int:
    if not session.table_exists("schema_version"):
        return 0
    return int(session.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_version") or 0)


def current_version(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Schema version stored in the database; 0 when there is no schema (or no file)."""
    if backend is None and path is None:
        from hokma.db.database import configured_backend

        backend = configured_backend()
    backend = backend or SqliteBackend(path or db_path())
    if not backend.exists():
        return 0
    session = backend.connect()
    try:
        return _version_of(session)
    finally:
        session.close()


def _prune_backups(directory: Path) -> None:
    files = sorted(directory.glob("hokma-v*.db"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


def migrate(path: Path | None = None, *, migrations_dir: Path | None = None, seed: bool = True,
            backend: Backend | None = None) -> MigrationResult:
    """Bring the database to the latest schema. Raises MigrationError (after rolling back) on any failure.

    `backend` selects the engine (Postgres); without it the SQLite file at `path` (default location) is used.
    Postgres is not copied to a file first: its backup is the server's."""
    if backend is None and path is None:
        from hokma.db.database import configured_backend

        backend = configured_backend()                            # Postgres when the owner switched to it
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    where = str(path) if backend.engine == "sqlite" else repr(backend)
    files = discover(migrations_dir)
    latest = files[-1][0] if files else 0
    try:
        result = MigrationResult(created=not backend.exists())
        session = backend.connect()
    except Exception as exc:                                      # unreadable folder, corrupt file...
        raise MigrationError(t("db.migrate.open_failed", path=where, error=exc)) from exc
    try:
        try:
            current = _version_of(session)
        except Exception as exc:                                  # not a database at all
            raise MigrationError(t("db.migrate.open_failed", path=where, error=exc)) from exc
        if current > latest:
            raise MigrationError(t("db.migrate.newer", found=current, known=latest), current)
        pending = [f for f in files if f[0] > current]

        if pending and not result.created and backend.engine == "sqlite":
            session.close()                                       # let the backup copy a quiet file
            result.backup = _backup(backend, path, current)
            session = backend.connect()

        for version, name, file in pending:
            _apply(session, backend, version, name, file, result)
        result.version = _version_of(session)

        if seed:
            from hokma.db.seeds import run_seeds

            session.begin(write=True)
            try:
                run_seeds(session)
                session.commit()
            except Exception as exc:
                session.rollback()
                raise MigrationError(t("db.migrate.seed_failed", error=exc)) from exc
    finally:
        session.close()
    return result


def _backup(backend: Backend, path: Path, version: int) -> Path:
    directory = backups_dir(path)
    stamp = utcnow().replace("-", "").replace(":", "").replace(" ", "-")
    target = directory / f"hokma-v{version}-{stamp}.db"
    n = 1
    while target.exists():
        n += 1
        target = directory / f"hokma-v{version}-{stamp}-{n}.db"
    try:
        backend.backup(target)
    except Exception as exc:
        raise MigrationError(t("db.migrate.backup_failed", error=exc), version) from exc
    _prune_backups(directory)
    return target


def _apply(session, backend: Backend, version: int, name: str, file: Path, result: MigrationResult) -> None:
    script = render(file.read_text(encoding="utf-8"), backend)
    session.begin(write=True)
    try:
        if _version_of(session) >= version:                       # another process applied it while we waited
            session.rollback()
            return
        for statement in backend.split_statements(script):
            session.execute(statement)
        session.execute("INSERT INTO schema_version (version) VALUES (:v)", {"v": version})
        session.commit()
    except Exception as exc:
        session.rollback()
        detail = exc.args[0] if isinstance(exc, DbError) and exc.args else exc
        raise MigrationError(t("db.migrate.failed", file=file.name, error=detail), version) from exc
    result.applied.append(version)

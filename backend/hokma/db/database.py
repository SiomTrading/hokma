"""Database: opens units of work. A Unit bundles every repository over one connection and one transaction."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hokma.db.backend import Backend, Session, SqliteBackend
from hokma.db.dbconfig import load_config
from hokma.db.paths import db_path
from hokma.db.repos.agents import AgentsRepo
from hokma.db.repos.approvals import ApprovalsRepo
from hokma.db.repos.conversations import AttachmentsRepo, ConversationsRepo, MessagesRepo
from hokma.db.repos.extensions import ExtensionRequestsRepo, ExtensionsRepo, ScheduledJobsRepo
from hokma.db.repos.governance import BugsRepo, DecisionsRepo, GuardEventsRepo, JournalRepo
from hokma.db.repos.misc import OnboardingRepo, UsageRepo
from hokma.db.repos.profile import LicenseChecksRepo, ProfileRepo
from hokma.db.repos.projects import ProjectsRepo
from hokma.db.repos.tasks import TaskCommentsRepo, TaskEventsRepo, TaskLinksRepo, TaskRunsRepo, TasksRepo


class Unit:
    """Every repository sharing one session. Created by `Database.read()` / `Database.write()`."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.profile = ProfileRepo(session)
        self.license_checks = LicenseChecksRepo(session)
        self.agents = AgentsRepo(session)
        self.projects = ProjectsRepo(session)
        self.conversations = ConversationsRepo(session)
        self.messages = MessagesRepo(session)
        self.attachments = AttachmentsRepo(session)
        self.tasks = TasksRepo(session)
        self.task_links = TaskLinksRepo(session)
        self.task_runs = TaskRunsRepo(session)
        self.task_events = TaskEventsRepo(session)
        self.task_comments = TaskCommentsRepo(session)
        self.approvals = ApprovalsRepo(session)
        self.decisions = DecisionsRepo(session)
        self.journal = JournalRepo(session)
        self.guard_events = GuardEventsRepo(session)
        self.bugs = BugsRepo(session)
        self.extensions = ExtensionsRepo(session)
        self.extension_requests = ExtensionRequestsRepo(session)
        self.scheduled_jobs = ScheduledJobsRepo(session)
        self.onboarding = OnboardingRepo(session)
        self.usage = UsageRepo(session)


class Database:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    @property
    def engine(self) -> str:
        return self.backend.engine

    @property
    def path(self) -> Path | None:
        """The SQLite file; None on Postgres."""
        return getattr(self.backend, "path", None)

    @contextmanager
    def _unit(self, write: bool) -> Iterator[Unit]:
        session = self.backend.connect()
        try:
            session.begin(write=write)
            unit = Unit(session)
            try:
                yield unit
            except BaseException:
                session.rollback()
                raise
            else:
                if write:
                    session.commit()
                else:
                    session.rollback()
        finally:
            session.close()

    def read(self):
        """Read-only unit of work (rolled back at exit)."""
        return self._unit(False)

    def write(self):
        """Write unit of work: commit at exit, rollback on any exception."""
        return self._unit(True)

    def close(self) -> None:
        """Connections live as long as a unit; nothing is held between units."""


def open_database(path: Path | str | None = None) -> Database:
    return Database(SqliteBackend(Path(path) if path else db_path()))


def open_postgres(url: str) -> Database:
    from hokma.db.postgres import PostgresBackend

    return Database(PostgresBackend(url))


def configured_backend() -> Backend:
    """The engine the owner chose (`<data>/database.json`): Postgres when set, else the SQLite file."""
    config = load_config()
    if config.engine == "postgres" and config.url:
        from hokma.db.postgres import PostgresBackend

        return PostgresBackend(config.url)
    return SqliteBackend(db_path())


def open_configured() -> Database:
    return Database(configured_backend())


_singleton: Database | None = None
_singleton_key: tuple | None = None
_lock = threading.Lock()


def get_database() -> Database:
    """Process-wide Database for the configured engine. Re-created if HOKMA_DATA_DIR or the engine choice changed."""
    global _singleton, _singleton_key
    with _lock:
        config = load_config()
        key = ("postgres", config.url) if config.engine == "postgres" else ("sqlite", str(db_path()))
        if _singleton is None or _singleton_key != key:
            _singleton = open_configured()
            _singleton_key = key
        return _singleton

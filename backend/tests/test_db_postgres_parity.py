"""The database test suites of D1 (triggers, tasks, repositories, governance, chat), run again against a real
Postgres: a case that passes on SQLite and fails here (or the other way round) is a bug (SPEC-003 2.4).

Each imported test keeps its own module's helpers; only the `db` fixture is replaced by a Postgres-backed one.
"""
import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from test_db_chat_and_cli import *  # noqa: F401,F403
from test_db_governance import *  # noqa: F401,F403
from test_db_repos_core import *  # noqa: F401,F403
from test_db_tasks import *  # noqa: F401,F403
from test_db_triggers import *  # noqa: F401,F403

from hokma.db import get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config


@pytest.fixture
def db(env, pg_url_migrated):  # noqa: F811
    """Same as dbsupport.db, but the data folder points to a fresh Postgres database."""
    save_config(DatabaseConfig("postgres", pg_url_migrated))
    migrate()
    database = get_database()
    assert database.engine == "postgres"
    with database.write() as u:
        for slug in ("hokma", "joel", "vera", "iris"):
            u.agents.upsert_kit(slug, slug.capitalize(), None, "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return database
